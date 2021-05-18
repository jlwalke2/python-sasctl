#!/usr/bin/env python
# encoding: utf-8
#
# Copyright © 2019, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Commonly used tasks in the analytics life cycle."""

import io
import json
import logging
import math
import pickle  # skipcq BAN-B301
import os
import re
import sys
import warnings
from collections.abc import Sequence

import six
from six.moves.urllib.error import HTTPError

from . import utils
from .core import RestObj, current_session, get, get_link, request_link
from .exceptions import AuthorizationError, ServiceUnavailableError
from .services import cas_management as cm
from .services import data_sources as ds
from .services import ml_pipeline_automation as mpa
from .services import model_management as mm
from .services import model_publish as mp
from .services import model_repository as mr
from .utils.pymas import from_pickle
from .utils.misc import installed_packages
from .utils.metrics import lift_statistics, roc_statistics, fit_statistics
from .utils.models import ModelInfo


logger = logging.getLogger(__name__)

# As of Viya 3.4 model registration fails if character fields are longer
# than 1024 characters
_DESC_MAXLEN = 1024

# As of Viya 3.4 model registration fails if user-defined properties are
# longer than 512 characters.
_PROP_VALUE_MAXLEN = 512
_PROP_NAME_MAXLEN = 60


def _property(k, v):
    return {'name': str(k)[:_PROP_NAME_MAXLEN], 'value': str(v)[:_PROP_VALUE_MAXLEN]}


def _sklearn_to_dict(model):
    # Convert Scikit-learn values to built-in Model Manager values
    mappings = {
        'LogisticRegression': 'Logistic regression',
        'LinearRegression': 'Linear regression',
        'SVC': 'Support vector machine',
        'GradientBoostingClassifier': 'Gradient boosting',
        'GradientBoostingRegressor': 'Gradient boosting',
        'XGBClassifier': 'Gradient boosting',
        'XGBRegressor': 'Gradient boosting',
        'RandomForestClassifier': 'Forest',
        'DecisionTreeClassifier': 'Decision tree',
        'DecisionTreeRegressor': 'Decision tree',
        'classifier': 'classification',
        'regressor': 'prediction',
    }

    if hasattr(model, '_final_estimator'):
        estimator = model._final_estimator
    else:
        estimator = model
    estimator = type(estimator).__name__

    # Standardize algorithm names
    algorithm = mappings.get(estimator, estimator)

    # Standardize regression/classification terms
    analytic_function = mappings.get(model._estimator_type, model._estimator_type)

    if analytic_function == 'classification' and 'logistic' in algorithm.lower():
        target_level = 'Binary'
    elif analytic_function == 'prediction' and (
        'regressor' in estimator.lower() or 'regression' in algorithm.lower()
    ):
        target_level = 'Interval'
    else:
        target_level = None

    # Can tell if multi-class .multi_class
    result = dict(
        description=str(model)[:_DESC_MAXLEN],
        algorithm=algorithm,
        scoreCodeType='ds2MultiType',
        trainCodeType='Python',
        targetLevel=target_level,
        function=analytic_function,
        tool='Python %s.%s' % (sys.version_info.major, sys.version_info.minor),
        properties=[_property(k, v) for k, v in model.get_params().items()],
    )

    return result


def _create_project(project_name, model, repo, input_vars=None, output_vars=None):
    """Creates a project based on the model specifications.

    Parameters
    ----------
    project_name : str
        Name of the project to be created
    model : dict
        Model information
    repo : str or dict
        Repository in which to create the project
    input_vars : list
        Input variables formatted as {'name': 'varname'}
    output_vars
        Output variables formatted as {'name': 'varname'}

    Returns
    -------
    RestObj
        The created project
    """
    properties = {k: model[k] for k in model if k in ('function', 'targetLevel')}

    function = model.get('function', '').lower()
    algorithm = model.get('algorithm', '').lower()

    # Get input & output variable lists
    # Note: copying lists to avoid altering original
    input_vars = input_vars or model.get('inputVariables', [])
    output_vars = output_vars or model.get('outputVariables', [])[:]
    input_vars = input_vars[:]
    output_vars = output_vars[:]

    # Set prediction or eventProbabilityVariable
    if output_vars:
        if function == 'prediction':
            properties['predictionVariable'] = output_vars[0]['name']
        else:
            properties['eventProbabilityVariable'] = output_vars[0]['name']

    # Set targetLevel
    if properties.get('targetLevel') is None:
        if function == 'classification' and 'logistic' in algorithm:
            properties['targetLevel'] = 'Binary'
        elif function == 'prediction' and 'regression' in algorithm:
            properties['targetLevel'] = 'Interval'
        else:
            properties['targetLevel'] = None

    project = mr.create_project(
        project_name, repo, variables=input_vars + output_vars, **properties
    )

    # As of Viya 3.4 the 'predictionVariable' and 'eventProbabilityVariable'
    # parameters are not set during project creation.  Update the project if
    # necessary.
    needs_update = False
    for p in ('predictionVariable', 'eventProbabilityVariable'):
        if project.get(p) != properties.get(p):
            project[p] = properties.get(p)
            needs_update = True

    if needs_update:
        project = mr.update_project(project)

    return project


def build_pipeline(data, target, name,
                   description=None,
                   table_name=None,
                   caslib=None,
                   max_models=None):
    """Automatically build a machine learning pipeline on a dataset.

    Parameters
    ----------
    data : str, DataFrame, CASTable or dict
        Input data for training models.  May be one of the following:
         - path to a file, in which case file will be uploaded to CAS.
         - a DataFrame instance, in which case the data will be uploaded to CAS.
         - an existing CASTable instance.
         - dict representation of a table as returned by `cas_management` or
           `data_sources` service.
    target : str
        Name of column in `data` containing the target variable.
    name : str
        Name of the project.
    description : str, optional
        A description of the project.
    table_name : str, optional
        Name of table to create in CAS when uploading `data`.  Required if
        `data` is a file path or DataFrame, otherwise ignored.
    caslib : str or dict optional
        caslib in which to table will be created when uploading `data`.
    max_models : int, optional
        Maximum number of models to train.

    Returns
    -------
    RestObj
        Project metadata

    Examples
    --------
    build_pipeline('examples/data/iris.csv', 'Species', 'Example Iris Pipeline')

    Raises
    ------
    ServiceUnavailableError
        If the ML Pipeline Automation service is unavailable.

    Notes
    -----
    The `data` table to be used as input must be globally-scoped (as opposed to
    session-scoped).

    """

    if data is None:
        raise ValueError("Parameter 'data' is required.  Received 'None'.")

    if not mpa.is_available():
        raise ServiceUnavailableError(mpa)

    # Local dataframe (either Pandas or SAS) will have .to_csv().  BUT CASTables
    # also have a .to_csv(), but there's no reason to download the data from
    # CAS just to write to CSV on disk and then upload back to CAS.
    if hasattr(data, 'to_csv') and not hasattr(data, 'get_connection'):
        csv_buffer = io.BytesIO()
        csv_buffer.write(data.to_csv(index=False).encode())
        csv_buffer.seek(0)

        if table_name is None:
            raise ValueError("'table_name' is a required parameter when "
                             "uploading a DataFrame.")

        # Upload the table to CAS
        data = cm.upload_file(csv_buffer, table_name, caslib=caslib, format_='csv')

    # If data is a string, assume it's a path to a file that can be uploaded
    elif isinstance(data, six.string_types):
        path = os.path.abspath(os.path.expanduser(data))
        if os.path.isfile(path):

            # Use the file name as the table name (minus file extension)
            if table_name is None:
                filename = os.path.split(path)[-1]
                table_name = os.path.splitext(filename)[0]

            data = cm.upload_file(data, table_name, caslib=caslib)

    # By now data should be uploaded (if necessary) and `data` should be a dict
    # or CASTable.  Retrieve the table's URI.
    table_uri = ds.table_uri(data)

    # Create the project
    project = mpa.create_project(table_uri, target, name,
                                 description=description,
                                 max_models=max_models)

    return project


def register_model(
    model,
    name,
    project,
    repository=None,
    input=None,
    version=None,
    files=None,
    force=False,
    data=None,
    train=None,
    test=None,
    valid=None,
    record_packages=True,
):
    """Register a model in the model repository.

    Parameters
    ----------
    model : swat.CASTable or sklearn.BaseEstimator
        The model to register.  If an instance of ``swat.CASTable`` the table
        is assumed to hold an ASTORE, which will be downloaded and used to
        construct the model to register.  If a scikit-learn estimator, the
        model will be pickled and uploaded to the registry and score code will
        be generated for publishing the model to MAS.
    name : str
        Designated name for the model in the repository.
    project : str or dict
        The name or id of the project, or a dictionary representation of
        the project.
    repository : str or dict, optional
        The name or id of the repository, or a dictionary representation of
        the repository.  If omitted, the default repository will be used.
    input : DataFrame, type, list of type, or dict of str: type, optional
        The expected type for each input value of the target function.
        Can be omitted if target function includes type hints.  If a DataFrame
        is provided, the columns will be inspected to determine type
        information.  If a single type is provided, all columns will be assumed
        to be that type, otherwise a list of column types or a dictionary of
        column_name: type may be provided.
    version : {'new', 'latest', int}, optional
        Version number of the project in which the model should be created.
        Defaults to 'new'.
    files : list
        A list of dictionaries of the form
        {'name': filename, 'file': filecontent}.
        An optional 'role' key is supported for designating a file as score
        code, astore, etc.
    force : bool, optional
        Create dependencies such as projects and repositories if they do not
        already exist.
    data : array_like or (array_like, array_like), optional
        Example input or (input, output) data for the model.  Required to determine model
        input and output variables and to enable model execution by SAS.
    train : (array_like, array_like), optional
        A tuple of the training inputs and target output.
    valid : (array_like, array_like), optional
        A tuple of the validation inputs and target output.
    test : (array_like, array_like), optional
        A tuple of the test inputs and target output.
    record_packages : bool, optional
        Capture Python packages registered in the environment.  Defaults to
        True.  Ignored if `model` is not a Python object.

    Returns
    -------
    model : RestObj
        The newly registered model as an instance of ``RestObj``

    Notes
    -----
    If the specified model is a CAS table the model data and metadata will be
    written to a temporary zip file and then imported using
    model_repository.import_model_from_zip.

    If the specified model is from the Scikit-Learn package, the model will be
    created using model_repository.create_model and any additional files will
    be uploaded as content.

    .. versionchanged:: v1.3
        Create requirements.txt with installed packages.

    .. versionchanged:: v1.4.5
        Added `record_packages` parameter.

    .. versionchanged:: v1.6
        Added `data` parameter

    """
    if input is not None:
        warnings.warn("The 'input' parameter has been deprecated and will be removed in a "
                      "future version.  Please use the 'data' parameter instead.",
                      DeprecationWarning)
        if data is None:
            data = input

    # If passed X or (X, ) instead of (X, y), convert to (X, None)
    if not isinstance(data, Sequence) or len(data) == 1:
        data = (data, None)

    # TODO: Create new version if model already exists

    # If version not specified, default to creating a new version
    version = version or 'new'

    files = files or []

    # Find the project if it already exists
    p = mr.get_project(project) if project is not None else None

    # Do we need to create the project first?
    create_project = bool(p is None and force is True)

    if p is None and not create_project:
        raise ValueError("Project '{}' not found".format(project))

    # Use default repository if not specified
    try:
        if repository is None:
            repo_obj = mr.default_repository()
        else:
            repo_obj = mr.get_repository(repository)
    except HTTPError as e:
        if e.code == 403:
            raise AuthorizationError(
                'Unable to register model.  User account '
                'does not have read permissions for the '
                '/modelRepository/repositories/ URL. '
                'Please contact your SAS Viya '
                'administrator.'
            )
        raise e

    # Unable to find or create the repo.
    if repo_obj is None and repository is None:
        raise ValueError("Unable to find a default repository")

    if repo_obj is None:
        raise ValueError("Unable to find repository '{}'".format(repository))

    # If model is a CASTable then assume it holds an ASTORE model.
    # Import these via a ZIP file.
    if 'swat.cas.table.CASTable' in str(type(model)):
        zipfile = utils.create_package(model, input=data)

        if create_project:
            outvar = []
            invar = []
            import zipfile as zp
            import copy

            zipfilecopy = copy.deepcopy(zipfile)
            tmpzip = zp.ZipFile(zipfilecopy)
            if "outputVar.json" in tmpzip.namelist():
                outvar = json.loads(
                    tmpzip.read("outputVar.json").decode('utf=8')
                )  # added decode for 3.5 and older
                for tmp in outvar:
                    tmp.update({'role': 'output'})
            if "inputVar.json" in tmpzip.namelist():
                invar = json.loads(
                    tmpzip.read("inputVar.json").decode('utf-8')
                )  # added decode for 3.5 and older
                for tmp in invar:
                    if tmp['role'] != 'input':
                        tmp['role'] = 'input'

            if 'ModelProperties.json' in tmpzip.namelist():
                model_props = json.loads(
                    tmpzip.read('ModelProperties.json').decode('utf-8')
                )
            else:
                model_props = {}

            project = _create_project(project, model_props, repo_obj, invar, outvar)

        model = mr.import_model_from_zip(name, project, zipfile, version=version)
        return model

    if isinstance(model, ModelInfo):
        # TODO: allow input of ModelInfo instance
        pass


    # If the model is an scikit-learn model, generate the model dictionary
    # from it and pickle the model for storage
    if all(hasattr(model, attr) for attr in ['_estimator_type', 'get_params']):
        # Pickle the model so we can store it
        model_pkl = pickle.dumps(model)
        files.append({'name': 'model.pkl', 'file': model_pkl, 'role': 'Python Pickle'})

        # Save actual model instance
        model_obj = model
        model_info = ModelInfo(model)
        model_info.name = name

        if any(x is not None for x in (train, test, valid)):
            for ds in (train, test, valid):
                if ds is not None:
                    model_info.set_variables(*ds)

                    if data is None:
                        data = ds
                    break
        else:
            model_info.set_variables(*data)

        model = model_info.to_dict()
        model['scoreCodeType'] = 'ds2MultiType'  # Change from Python since sasctl current doing conversion.

        # Calculate and include various model statistics.
        if any(x is not None for x in (train, test, valid)):
            for name, func in (
                ('dmcas_lift.json', lift_statistics),
                ('dmcas_fitstat.json', fit_statistics),
                ('dmcas_roc.json', roc_statistics),
            ):
                if not any(f['name'] == name for f in files):
                    logger.debug(
                        'Calling %s with train=%s, test=%s, valid=%s',
                        func,
                        type(train),
                        type(test),
                        type(valid),
                    )
                    stats = func(model_obj, train=train, test=test, valid=valid)
                    logger.debug('Writing model statistics to %s:  %s', name, stats)
                    files.append({'name': name, 'file': json.dumps(stats, indent=2)})

        # Get package versions in environment
        if record_packages and not any(f['name'] == 'requirements.txt' for f in files):
            packages = installed_packages()
            if packages is not None:
                model.setdefault('properties', [])

                # Define a custom property to capture each package version
                # NOTE: some packages may not conform to the 'name==version' format
                #  expected here (e.g those installed with pip install -e). Such
                #  packages also generally contain characters that are not allowed
                # in custom properties, so they are excluded here.
                for p in packages:
                    if '==' in p:
                        n, v = p.split('==')
                        model['properties'].append(_property('env_%s' % n, v))

                # Generate and upload a requirements.txt file
                files.append({'name': 'requirements.txt', 'file': '\n'.join(packages)})

        # Generate PyMAS wrapper
        try:
            from .utils.pymas.core import from_model_info
            mas_module = from_model_info(model_info)
            # mas_module = from_pickle(
            #     model_pkl, model_info.function_names, input_types=data[0], array_input=True
            # )

            # Include score code files from ESP and MAS
            files.append(
                {
                    'name': 'dmcas_packagescorecode.sas',
                    'file': mas_module.score_code(),
                    'role': 'Score Code',
                }
            )
            files.append(
                {
                    'name': 'dmcas_epscorecode.sas',
                    'file': mas_module.score_code(dest='CAS'),
                    'role': 'score',
                }
            )
            files.append(
                {
                    'name': 'python_wrapper.py',
                    'file': mas_module.score_code(dest='Python'),
                }
            )

            model['inputVariables'] = [
                var.as_model_metadata() for var in mas_module.variables if not var.out
            ]

            model['outputVariables'] = [
                var.as_model_metadata() for var in mas_module.variables if var.out
            ]
        except ValueError:
            # PyMAS creation failed, most likely because input data wasn't
            # provided
            logger.exception('Unable to inspect model %s', model)

            warnings.warn(
                'Unable to determine input/output variables. '
                ' Model variables will not be specified and some '
                'model functionality may not be available.'
            )
    else:
        # Otherwise, the model better be a dictionary of metadata
        if not isinstance(model, dict):
            raise TypeError(
                "Expected an instance of '%r' but received '%r'." % ({}, model)
            )

    if create_project:
        project = _create_project(project, model, repo_obj)

    # If replacing an existing version, make sure the model version exists
    if str(version).lower() != 'new':
        # Update an existing model with new files
        model_obj = mr.get_model(name)
        if model_obj is None:
            raise ValueError(
                "Unable to update version '%s' of model '%s.  "
                "Model not found." % (version, name)
            )
        model = mr.create_model_version(name)
        mr.delete_model_contents(model)
    else:
        # Assume new model to create
        model = mr.create_model(model, project)

    if not isinstance(model, RestObj):
        raise TypeError(
            "Model should be an instance of '%r' but received '%r' "
            "instead." % (RestObj, model)
        )

    # Upload any additional files
    for file in files:
        if isinstance(file, dict):
            mr.add_model_content(model, **file)
        else:
            mr.add_model_content(model, file)

    return model


def publish_model(
    model, destination, code=None, max_retries=60, replace=False, **kwargs
):
    """Publish a model to a configured publishing destination.

    Parameters
    ----------
    model : str or dict
        The name or id of the model, or a dictionary representation of
        the model.
    destination : str
    code : optional
    max_retries : int, optional
    replace : bool, optional
        Whether to overwrite the model if it already exists in
        the `destination`
    kwargs : optional
        additional arguments will be passed to the underlying publish
        functions.

    Returns
    -------
    RestObj
        The published model

    Notes
    -----
    If no code is specified, the model is assumed to be already registered in
    the model repository and Model Manager's publishing functionality will be
    used.

    Otherwise, the model publishing API will be used.

    See Also
    --------
    :meth:`model_management.publish_model <.ModelManagement.publish_model>`
    :meth:`model_publish.publish_model <.ModelPublish.publish_model>`


    .. versionchanged:: 1.1.0
       Added `replace` option.

    """

    def submit_request():
        # Submit a publishing request
        if code is None:
            dest_obj = mp.get_destination(destination)

            if dest_obj and dest_obj.destinationType == "cas":
                publish_req = mm.publish_model(
                    model, destination, force=replace, reload_model_table=True
                )
            else:
                publish_req = mm.publish_model(model, destination, force=replace)
        else:
            publish_req = mp.publish_model(model, destination, code=code, **kwargs)

        # A successfully submitted request doesn't mean a successfully
        # published model.  Response for publish request includes link to
        # check publish log
        result = mr._monitor_job(publish_req, max_retries=max_retries)
        return result

    # Submit and wait for status
    job = submit_request()

    # If model was successfully published and it isn't a MAS module, we're done
    if (
        job.state.lower() == 'completed'
        and job.destination.destinationType != 'microAnalyticService'
    ):
        return request_link(job, 'self')

    # If MAS publish failed and replace=True, attempt to delete the module
    # and republish
    if (
        job.state.lower() == 'failed'
        and replace
        and job.destination.destinationType == 'microAnalyticService'
    ):
        from .services import microanalytic_score as mas

        mas.delete_module(job.publishName)

        # Resubmit the request
        job = submit_request()

    # Raise exception if still failing
    if job.state.lower() == 'failed':
        log = request_link(job, 'publishingLog')
        raise RuntimeError("Failed to publish model '%s': %s" % (model, log.log))

    # Raise exception if unknown status received
    if job.state.lower() != 'completed':
        raise RuntimeError(
            "Model publishing job in an unknown state: '%s'" % job.state.lower()
        )

    log = request_link(job, 'publishingLog')
    msg = log.get('log').lstrip('SUCCESS===')

    # As of Viya 3.4 MAS converts module names to lower case.
    # Since we can't rely on the request module name being preserved, try to
    # parse the URL out of the response so we can retrieve the created module.
    module_url = _parse_module_url(msg)
    if module_url is None:
        raise Exception('Unable to retrieve module URL from publish log.')

    module = get(module_url)

    if 'application/vnd.sas.microanalytic.module' in module._headers['content-type']:
        # Bind Python methods to the module instance that will execute the
        # corresponding MAS module step.
        from sasctl.services import microanalytic_score as mas

        return mas.define_steps(module)
    return module


def update_model_performance(data, model, label, refresh=True):
    """Upload data for calculating model performance metrics.

    Model performance and data distributions can be tracked over time by
    designating one or more tables that contain input data and target values.
    Performance metrics can be updated by uploading a data set for a new time
    period and executing the performance definition.

    Parameters
    ----------
    data : Dataframe
    model : str or dict
        The name or id of the model, or a dictionary representation of
        the model.
    label : str
        The time period the data is from.  Should be unique and will be
        displayed on performance charts.  Examples: 'Q1', '2019', 'APR2019'.
    refresh : bool, optional
        Whether to execute the performance definition and refresh results with
        the new data.

    Returns
    -------
    CASTable
        The CAS table containing the performance data.

    See Also
    --------
     :meth:`model_management.create_performance_definition <.ModelManagement.create_performance_definition>`

    .. versionadded:: v1.3

    """
    try:
        import swat
    except ImportError:
        raise RuntimeError(
            "The 'swat' package is required to save model " "performance data."
        )

    # Default to true
    refresh = True if refresh is None else refresh

    model_obj = mr.get_model(model)

    if model_obj is None:
        raise ValueError('Model %s was not found.' % model)

    project = mr.get_project(model_obj.projectId)

    if project.get('function', '').lower() not in ('prediction', 'classification'):
        raise ValueError(
            "Performance monitoring is currently supported for "
            "regression and binary classification projects.  "
            "Received project with '%s' function.  Should be "
            "'Prediction' or 'Classification'." % project.get('function')
        )

    if project.get('targetLevel', '').lower() not in ('interval', 'binary'):
        raise ValueError(
            "Performance monitoring is currently supported for "
            "regression and binary classification projects.  "
            "Received project with '%s' target level.  Should be "
            "'Interval' or 'Binary'." % project.get('targetLevel')
        )

    if (
        project.get('predictionVariable', '') == ''
        and project.get('function', '').lower() == 'prediction'
    ):
        raise ValueError(
            "Project '%s' does not have a prediction variable " "specified." % project
        )

    if (
        project.get('eventProbabilityVariable', '') == ''
        and project.get('function', '').lower() == 'classification'
    ):
        raise ValueError(
            "Project '%s' does not have an Event Probability variable "
            "specified." % project
        )

    # Find the performance definition for the model
    # As of Viya 3.4, no way to search by model or project
    perf_def = None
    for p in mm.list_performance_definitions():
        if project.id in p.projectId:
            perf_def = p
            break

    if perf_def is None:
        raise ValueError(
            "Unable to find a performance definition for model " "'%s'" % model
        )

    # Check where performance datasets should be uploaded
    cas_id = perf_def['casServerId']
    caslib = perf_def['dataLibrary']
    table_prefix = perf_def['dataPrefix']

    # All input variables must be present
    missing_cols = [col for col in perf_def.inputVariables if col not in data.columns]
    if missing_cols:
        raise ValueError(
            "The following columns were expected but not found in "
            "the data set: %s" % ', '.join(missing_cols)
        )

    # If CAS is not executing the model then the output variables must also be
    # provided
    if not perf_def.scoreExecutionRequired:
        missing_cols = [
            col for col in perf_def.outputVariables if col not in data.columns
        ]
        if missing_cols:
            raise ValueError(
                "The following columns were expected but not found in the data "
                "set: %s" % ', '.join(missing_cols)
            )

    sess = current_session()
    url = '{}://{}/{}-http/'.format(sess._settings['protocol'], sess.hostname, cas_id)
    regex = r'{}_(\d+)_.*_{}'.format(table_prefix, model_obj.id)

    # Save the current setting before overwriting
    orig_sslreqcert = os.environ.get('SSLREQCERT')

    # If SSL connections to microservices are not being verified, don't attempt
    # to verify connections to CAS - most likely certs are not in place.
    if not sess.verify:
        os.environ['SSLREQCERT'] = 'no'

    # Upload the performance data to CAS
    with swat.CAS(
        url, username=sess.username, password=sess._settings['password']
    ) as s:

        s.setsessopt(messagelevel='warning')

        with swat.options(exception_on_severity=2):
            caslib_info = s.table.tableinfo(caslib=caslib)

        all_tables = getattr(caslib_info, 'TableInfo', None)
        if all_tables is not None:
            # Find tables with similar names
            perf_tables = all_tables.Name.str.extract(
                regex, flags=re.IGNORECASE, expand=False
            )

            # Get last-used sequence number
            last_seq = perf_tables.dropna().astype(int).max()
            next_seq = 1 if math.isnan(last_seq) else last_seq + 1
        else:
            next_seq = 1

        table_name = '{prefix}_{sequence}_{label}_{model}'.format(
            prefix=table_prefix, sequence=next_seq, label=label, model=model_obj.id
        )

        with swat.options(exception_on_severity=2):
            # Table must be promoted so performance jobs can access.
            result = s.upload(
                data, casout=dict(name=table_name, caslib=caslib, promote=True)
            )

            if not hasattr(result, 'casTable'):
                raise RuntimeError('Unable to upload performance data to CAS.')

            tbl = result.casTable

    # Restore the original value
    if orig_sslreqcert is not None:
        os.environ['SSLREQCERT'] = orig_sslreqcert

    # Execute the definition if requested
    if refresh:
        mm.execute_performance_definition(perf_def)

    return tbl


def _parse_module_url(msg):
    try:
        details = json.loads(msg)

        module_url = get_link(details, 'module')
        module_url = module_url.get('href')
    except json.JSONDecodeError:
        match = re.search(r'(?:rel=module, href=(.*?),)', msg)               # Vya 3.5
        if match is None:
            match = re.search(r'(?:Rel: module URI: (.*?) MediaType)', msg)  # Format changed in Viya 4.0
        module_url = match.group(1) if match else None

    return module_url
