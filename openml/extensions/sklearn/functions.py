"""Convert scikit-learn estimators into an OpenMLFlows and vice versa."""

from collections import OrderedDict
import copy
from distutils.version import LooseVersion
import importlib
import inspect
import json
import json.decoder
import logging
import re
import warnings
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np
import scipy.stats.distributions
import sklearn.base
import sklearn.model_selection
from inspect import signature

import openml
from openml.flows import OpenMLFlow
from openml.exceptions import PyOpenMLError


if sys.version_info >= (3, 5):
    from json.decoder import JSONDecodeError
else:
    JSONDecodeError = ValueError


DEPENDENCIES_PATTERN = re.compile(
    r'^(?P<name>[\w\-]+)((?P<operation>==|>=|>)'
    r'(?P<version>(\d+\.)?(\d+\.)?(\d+)?(dev)?[0-9]*))?$'
)


SIMPLE_NUMPY_TYPES = [nptype for type_cat, nptypes in np.sctypes.items()
                      for nptype in nptypes if type_cat != 'others']
SIMPLE_TYPES = tuple([bool, int, float, str] + SIMPLE_NUMPY_TYPES)


def sklearn_to_flow(o: Any, parent_model: Any = None) -> Any:
    # Necessary to make pypy not complain about all the different possible return types
    rval = None  # type: Any

    # TODO: assert that only on first recursion lvl `parent_model` can be None
    if is_estimator(o):
        # is the main model or a submodel
        rval = _serialize_model(o)
    elif isinstance(o, (list, tuple)):
        # TODO: explain what type of parameter is here
        rval = [sklearn_to_flow(element, parent_model) for element in o]
        if isinstance(o, tuple):
            rval = tuple(rval)
    elif isinstance(o, SIMPLE_TYPES) or o is None:
        if isinstance(o, tuple(SIMPLE_NUMPY_TYPES)):
            o = o.item()
        # base parameter values
        rval = o
    elif isinstance(o, dict):
        # TODO: explain what type of parameter is here
        if not isinstance(o, OrderedDict):
            o = OrderedDict([(key, value) for key, value in sorted(o.items())])

        rval = OrderedDict()
        for key, value in o.items():
            if not isinstance(key, str):
                raise TypeError('Can only use string as keys, you passed '
                                'type %s for value %s.' %
                                (type(key), str(key)))
            key = sklearn_to_flow(key, parent_model)
            value = sklearn_to_flow(value, parent_model)
            rval[key] = value
        rval = rval
    elif isinstance(o, type):
        # TODO: explain what type of parameter is here
        rval = serialize_type(o)
    elif isinstance(o, scipy.stats.distributions.rv_frozen):
        rval = serialize_rv_frozen(o)
    # This only works for user-defined functions (and not even partial).
    # I think this is exactly what we want here as there shouldn't be any
    # built-in or functool.partials in a pipeline
    elif inspect.isfunction(o):
        # TODO: explain what type of parameter is here
        rval = serialize_function(o)
    elif is_cross_validator(o):
        # TODO: explain what type of parameter is here
        rval = _serialize_cross_validator(o)
    else:
        raise TypeError(o, type(o))

    return rval


def is_estimator(o: Any) -> bool:
    return hasattr(o, 'fit') and hasattr(o, 'get_params') and hasattr(o, 'set_params')


def is_cross_validator(o: Any) -> bool:
    return isinstance(o, sklearn.model_selection.BaseCrossValidator)


def flow_to_sklearn(
    o: Any,
    components: Optional[Dict] = None,
    initialize_with_defaults: bool = False,
    recursion_depth: int = 0,
) -> Any:
    """Initializes a sklearn model based on a flow.

    Parameters
    ----------
    o : mixed
        the object to deserialize (can be flow object, or any serialized
        parameter value that is accepted by)

    components : dict


    initialize_with_defaults : bool, optional (default=False)
        If this flag is set, the hyperparameter values of flows will be
        ignored and a flow with its defaults is returned.

    recursion_depth : int
        The depth at which this flow is called, mostly for debugging
        purposes

    Returns
    -------
    mixed

    """
    logging.info('-%s flow_to_sklearn START o=%s, components=%s, '
                 'init_defaults=%s' % ('-' * recursion_depth, o, components,
                                       initialize_with_defaults))
    depth_pp = recursion_depth + 1  # shortcut var, depth plus plus

    # First, we need to check whether the presented object is a json string.
    # JSON strings are used to encoder parameter values. By passing around
    # json strings for parameters, we make sure that we can flow_to_sklearn
    # the parameter values to the correct type.

    if isinstance(o, str):
        try:
            o = json.loads(o)
        except JSONDecodeError:
            pass

    if isinstance(o, dict):
        # Check if the dict encodes a 'special' object, which could not
        # easily converted into a string, but rather the information to
        # re-create the object were stored in a dictionary.
        if 'oml-python:serialized_object' in o:
            serialized_type = o['oml-python:serialized_object']
            value = o['value']
            if serialized_type == 'type':
                rval = deserialize_type(value)
            elif serialized_type == 'rv_frozen':
                rval = deserialize_rv_frozen(value)
            elif serialized_type == 'function':
                rval = deserialize_function(value)
            elif serialized_type == 'component_reference':
                assert components is not None  # Necessary for mypy
                value = flow_to_sklearn(value, recursion_depth=depth_pp)
                step_name = value['step_name']
                key = value['key']
                component = flow_to_sklearn(
                    components[key],
                    initialize_with_defaults=initialize_with_defaults,
                    recursion_depth=depth_pp
                )
                # The component is now added to where it should be used
                # later. It should not be passed to the constructor of the
                # main flow object.
                del components[key]
                if step_name is None:
                    rval = component
                elif 'argument_1' not in value:
                    rval = (step_name, component)
                else:
                    rval = (step_name, component, value['argument_1'])
            elif serialized_type == 'cv_object':
                rval = _deserialize_cross_validator(
                    value, recursion_depth=recursion_depth
                )
            else:
                raise ValueError('Cannot flow_to_sklearn %s' % serialized_type)

        else:
            rval = OrderedDict((flow_to_sklearn(key,
                                                components,
                                                initialize_with_defaults,
                                                recursion_depth=depth_pp),
                                flow_to_sklearn(value,
                                                components,
                                                initialize_with_defaults,
                                                recursion_depth=depth_pp))
                               for key, value in sorted(o.items()))
    elif isinstance(o, (list, tuple)):
        rval = [flow_to_sklearn(element,
                                components,
                                initialize_with_defaults,
                                depth_pp) for element in o]
        if isinstance(o, tuple):
            rval = tuple(rval)
    elif isinstance(o, (bool, int, float, str)) or o is None:
        rval = o
    elif isinstance(o, OpenMLFlow):
        if not (o.external_version.startswith('sklearn==')
                or ',sklearn==' in o.external_version):
            raise ValueError('Only sklearn flows can be reinstantiated')
        rval = _deserialize_model(o,
                                  initialize_with_defaults,
                                  recursion_depth=recursion_depth)
    else:
        raise TypeError(o)
    logging.info('-%s flow_to_sklearn END   o=%s, rval=%s'
                 % ('-' * recursion_depth, o, rval))
    return rval


def openml_param_name_to_sklearn(
    openml_parameter: openml.setups.OpenMLParameter,
    flow: OpenMLFlow,
) -> str:
    """
    Converts the name of an OpenMLParameter into the sklean name, given a flow.

    Parameters
    ----------
    openml_parameter: OpenMLParameter
        The parameter under consideration

    flow: OpenMLFlow
        The flow that provides context.

    Returns
    -------
    sklearn_parameter_name: str
        The name the parameter will have once used in scikit-learn
    """
    if not isinstance(openml_parameter, openml.setups.OpenMLParameter):
        raise ValueError('openml_parameter should be an instance of '
                         'OpenMLParameter')
    if not isinstance(flow, OpenMLFlow):
        raise ValueError('flow should be an instance of OpenMLFlow')

    flow_structure = flow.get_structure('name')
    if openml_parameter.flow_name not in flow_structure:
        raise ValueError('Obtained OpenMLParameter and OpenMLFlow do not '
                         'correspond. ')
    name = openml_parameter.flow_name  # for PEP8
    return '__'.join(flow_structure[name] + [openml_parameter.parameter_name])


def obtain_parameter_values(flow: OpenMLFlow, model: object = None) -> List[Dict[str, Any]]:
    """
    Extracts all parameter settings required for the flow from the model.
    If no explicit model is provided, the parameters will be extracted from `flow.model` instead.

    Parameters
    ----------
    flow : OpenMLFlow
        OpenMLFlow object (containing flow ids, i.e., it has to be downloaded from the server)

    model: object, optional (default=None)
        The model from which to obtain the parameter values. Must match the flow signature.
        If None, use the model specified in `OpenMLFlow.model`

    Returns
    -------
    list
        A list of dicts, where each dict has the following names:
         - oml:name (str): The OpenML parameter name
         - oml:value (mixed): A representation of the parameter value
         - oml:component (int): flow id to which the parameter belongs
    """

    openml.flows.functions._check_flow_for_server_id(flow)

    def get_flow_dict(_flow):
        flow_map = {_flow.name: _flow.flow_id}
        for subflow in _flow.components:
            flow_map.update(get_flow_dict(_flow.components[subflow]))
        return flow_map

    def extract_parameters(_flow, _flow_dict, component_model,
                           _main_call=False, main_id=None):
        def is_subcomponent_specification(values):
            # checks whether the current value can be a specification of
            # subcomponents, as for example the value for steps parameter
            # (in Pipeline) or transformers parameter (in
            # ColumnTransformer). These are always lists/tuples of lists/
            # tuples, size bigger than 2 and an OpenMLFlow item involved.
            if not isinstance(values, (tuple, list)):
                return False
            for item in values:
                if not isinstance(item, (tuple, list)):
                    return False
                if len(item) < 2:
                    return False
                if not isinstance(item[1], openml.flows.OpenMLFlow):
                    return False
            return True

        # _flow is openml flow object, _param dict maps from flow name to flow
        # id for the main call, the param dict can be overridden (useful for
        # unit tests / sentinels) this way, for flows without subflows we do
        # not have to rely on _flow_dict
        exp_parameters = set(_flow.parameters)
        exp_components = set(_flow.components)
        model_parameters = set([mp for mp in component_model.get_params()
                                if '__' not in mp])
        if len((exp_parameters | exp_components) ^ model_parameters) != 0:
            flow_params = sorted(exp_parameters | exp_components)
            model_params = sorted(model_parameters)
            raise ValueError('Parameters of the model do not match the '
                             'parameters expected by the '
                             'flow:\nexpected flow parameters: '
                             '%s\nmodel parameters: %s' % (flow_params,
                                                           model_params))

        _params = []
        for _param_name in _flow.parameters:
            _current = OrderedDict()
            _current['oml:name'] = _param_name

            current_param_values = openml.extensions.sklearn.functions.sklearn_to_flow(
                component_model.get_params()[_param_name])

            # Try to filter out components (a.k.a. subflows) which are
            # handled further down in the code (by recursively calling
            # this function)!
            if isinstance(current_param_values, openml.flows.OpenMLFlow):
                continue

            if is_subcomponent_specification(current_param_values):
                # complex parameter value, with subcomponents
                parsed_values = list()
                for subcomponent in current_param_values:
                    # scikit-learn stores usually tuples in the form
                    # (name (str), subcomponent (mixed), argument
                    # (mixed)). OpenML replaces the subcomponent by an
                    # OpenMLFlow object.
                    if len(subcomponent) < 2 or len(subcomponent) > 3:
                        raise ValueError('Component reference should be '
                                         'size {2,3}. ')

                    subcomponent_identifier = subcomponent[0]
                    subcomponent_flow = subcomponent[1]
                    if not isinstance(subcomponent_identifier, str):
                        raise TypeError('Subcomponent identifier should be '
                                        'string')
                    if not isinstance(subcomponent_flow,
                                      openml.flows.OpenMLFlow):
                        raise TypeError('Subcomponent flow should be string')

                    current = {
                        "oml-python:serialized_object": "component_reference",
                        "value": {
                            "key": subcomponent_identifier,
                            "step_name": subcomponent_identifier
                        }
                    }
                    if len(subcomponent) == 3:
                        if not isinstance(subcomponent[2], list):
                            raise TypeError('Subcomponent argument should be'
                                            'list')
                        current['value']['argument_1'] = subcomponent[2]
                    parsed_values.append(current)
                parsed_values = json.dumps(parsed_values)
            else:
                # vanilla parameter value
                parsed_values = json.dumps(current_param_values)

            _current['oml:value'] = parsed_values
            if _main_call:
                _current['oml:component'] = main_id
            else:
                _current['oml:component'] = _flow_dict[_flow.name]
            _params.append(_current)

        for _identifier in _flow.components:
            subcomponent_model = component_model.get_params()[_identifier]
            _params.extend(extract_parameters(_flow.components[_identifier],
                                              _flow_dict, subcomponent_model))
        return _params

    flow_dict = get_flow_dict(flow)
    model = model if model is not None else flow.model
    parameters = extract_parameters(flow, flow_dict, model,
                                    True, flow.flow_id)

    return parameters


def _serialize_model(model: Any) -> OpenMLFlow:
    """Create an OpenMLFlow.

    Calls `sklearn_to_flow` recursively to properly serialize the
    parameters to strings and the components (other models) to OpenMLFlows.

    Parameters
    ----------
    model : sklearn estimator

    Returns
    -------
    OpenMLFlow

    """

    # Get all necessary information about the model objects itself
    parameters, parameters_meta_info, subcomponents, subcomponents_explicit =\
        _extract_information_from_model(model)

    # Check that a component does not occur multiple times in a flow as this
    # is not supported by OpenML
    _check_multiple_occurence_of_component_in_flow(model, subcomponents)

    # Create a flow name, which contains all components in brackets, e.g.:
    # RandomizedSearchCV(Pipeline(StandardScaler,AdaBoostClassifier(DecisionTreeClassifier)),StandardScaler,AdaBoostClassifier(DecisionTreeClassifier))
    class_name = model.__module__ + "." + model.__class__.__name__

    # will be part of the name (in brackets)
    sub_components_names = ""
    for key in subcomponents:
        if key in subcomponents_explicit:
            sub_components_names += "," + key + "=" + subcomponents[key].name
        else:
            sub_components_names += "," + subcomponents[key].name

    if sub_components_names:
        # slice operation on string in order to get rid of leading comma
        name = '%s(%s)' % (class_name, sub_components_names[1:])
    else:
        name = class_name

    # Get the external versions of all sub-components
    external_version = _get_external_version_string(model, subcomponents)

    dependencies = '\n'.join([
        _format_external_version('sklearn', sklearn.__version__),
        'numpy>=1.6.1',
        'scipy>=0.9',
    ])

    sklearn_version = _format_external_version('sklearn', sklearn.__version__)
    sklearn_version_formatted = sklearn_version.replace('==', '_')
    flow = OpenMLFlow(name=name,
                      class_name=class_name,
                      description='Automatically created scikit-learn flow.',
                      model=model,
                      components=subcomponents,
                      parameters=parameters,
                      parameters_meta_info=parameters_meta_info,
                      external_version=external_version,
                      tags=['openml-python', 'sklearn', 'scikit-learn',
                            'python', sklearn_version_formatted,
                            # TODO: add more tags based on the scikit-learn
                            # module a flow is in? For example automatically
                            # annotate a class of sklearn.svm.SVC() with the
                            # tag svm?
                            ],
                      language='English',
                      # TODO fill in dependencies!
                      dependencies=dependencies)

    return flow


def _get_external_version_string(model: Any, sub_components: Dict[str, OpenMLFlow]) -> str:
    # Create external version string for a flow, given the model and the
    # already parsed dictionary of sub_components. Retrieves the external
    # version of all subcomponents, which themselves already contain all
    # requirements for their subcomponents. The external version string is a
    # sorted concatenation of all modules which are present in this run.
    model_package_name = model.__module__.split('.')[0]
    module = importlib.import_module(model_package_name)
    model_package_version_number = module.__version__  # type: ignore
    external_version = _format_external_version(model_package_name,
                                                model_package_version_number)
    openml_version = _format_external_version('openml', openml.__version__)
    external_versions = set()
    external_versions.add(external_version)
    external_versions.add(openml_version)
    for visitee in sub_components.values():
        for external_version in visitee.external_version.split(','):
            external_versions.add(external_version)
    return ','.join(list(sorted(external_versions)))


def _check_multiple_occurence_of_component_in_flow(
    model: Any,
    sub_components: Dict[str, OpenMLFlow],
) -> None:
    to_visit_stack = []  # type: List[OpenMLFlow]
    to_visit_stack.extend(sub_components.values())
    known_sub_components = set()  # type: Set[OpenMLFlow]
    while len(to_visit_stack) > 0:
        visitee = to_visit_stack.pop()
        if visitee.name in known_sub_components:
            raise ValueError('Found a second occurence of component %s when '
                             'trying to serialize %s.' % (visitee.name, model))
        else:
            known_sub_components.add(visitee.name)
            to_visit_stack.extend(visitee.components.values())


def _extract_information_from_model(
    model: Any,
) -> Tuple[
    'OrderedDict[str, Optional[str]]',
    'OrderedDict[str, Optional[Dict]]',
    'OrderedDict[str, OpenMLFlow]',
    Set,
]:
    # This function contains four "global" states and is quite long and
    # complicated. If it gets to complicated to ensure it's correctness,
    # it would be best to make it a class with the four "global" states being
    # the class attributes and the if/elif/else in the for-loop calls to
    # separate class methods

    # stores all entities that should become subcomponents
    sub_components = OrderedDict()  # type: OrderedDict[str, OpenMLFlow]
    # stores the keys of all subcomponents that should become
    sub_components_explicit = set()
    parameters = OrderedDict()  # type: OrderedDict[str, Optional[str]]
    parameters_meta_info = OrderedDict()  # type: OrderedDict[str, Optional[Dict]]

    model_parameters = model.get_params(deep=False)
    for k, v in sorted(model_parameters.items(), key=lambda t: t[0]):
        rval = sklearn_to_flow(v, model)

        def flatten_all(list_):
            """ Flattens arbitrary depth lists of lists (e.g. [[1,2],[3,[1]]] -> [1,2,3,1]). """
            for el in list_:
                if isinstance(el, (list, tuple)):
                    yield from flatten_all(el)
                else:
                    yield el

        # In case rval is a list of lists (or tuples), we need to identify two situations:
        # - sklearn pipeline steps, feature union or base classifiers in voting classifier.
        #   They look like e.g. [("imputer", Imputer()), ("classifier", SVC())]
        # - a list of lists with simple types (e.g. int or str), such as for an OrdinalEncoder
        #   where all possible values for each feature are described: [[0,1,2], [1,2,5]]
        is_non_empty_list_of_lists_with_same_type = (
            isinstance(rval, (list, tuple))
            and len(rval) > 0
            and isinstance(rval[0], (list, tuple))
            and all([isinstance(rval_i, type(rval[0])) for rval_i in rval])
        )

        # Check that all list elements are of simple types.
        nested_list_of_simple_types = (
            is_non_empty_list_of_lists_with_same_type
            and all([isinstance(el, SIMPLE_TYPES) for el in flatten_all(rval)])
        )

        if is_non_empty_list_of_lists_with_same_type and not nested_list_of_simple_types:
            # If a list of lists is identified that include 'non-simple' types (e.g. objects),
            # we assume they are steps in a pipeline, feature union, or base classifiers in
            # a voting classifier.
            parameter_value = list()  # type: List
            reserved_keywords = set(model.get_params(deep=False).keys())

            for sub_component_tuple in rval:
                identifier = sub_component_tuple[0]
                sub_component = sub_component_tuple[1]
                sub_component_type = type(sub_component_tuple)
                if not 2 <= len(sub_component_tuple) <= 3:
                    # length 2 is for {VotingClassifier.estimators,
                    # Pipeline.steps, FeatureUnion.transformer_list}
                    # length 3 is for ColumnTransformer
                    msg = 'Length of tuple does not match assumptions'
                    raise ValueError(msg)
                if not isinstance(sub_component, (OpenMLFlow, type(None))):
                    msg = 'Second item of tuple does not match assumptions. '\
                          'Expected OpenMLFlow, got %s' % type(sub_component)
                    raise TypeError(msg)

                if identifier in reserved_keywords:
                    parent_model = "{}.{}".format(model.__module__,
                                                  model.__class__.__name__)
                    msg = 'Found element shadowing official '\
                          'parameter for %s: %s' % (parent_model,
                                                    identifier)
                    raise PyOpenMLError(msg)

                if sub_component is None:
                    # In a FeatureUnion it is legal to have a None step

                    pv = [identifier, None]
                    if sub_component_type is tuple:
                        parameter_value.append(tuple(pv))
                    else:
                        parameter_value.append(pv)

                else:
                    # Add the component to the list of components, add a
                    # component reference as a placeholder to the list of
                    # parameters, which will be replaced by the real component
                    # when deserializing the parameter
                    sub_components_explicit.add(identifier)
                    sub_components[identifier] = sub_component
                    component_reference = OrderedDict()  # type: Dict[str, Union[str, Dict]]
                    component_reference['oml-python:serialized_object'] = 'component_reference'
                    cr_value = OrderedDict()  # type: Dict[str, Any]
                    cr_value['key'] = identifier
                    cr_value['step_name'] = identifier
                    if len(sub_component_tuple) == 3:
                        cr_value['argument_1'] = sub_component_tuple[2]
                    component_reference['value'] = cr_value
                    parameter_value.append(component_reference)

            # Here (and in the elif and else branch below) are the only
            # places where we encode a value as json to make sure that all
            # parameter values still have the same type after
            # deserialization
            if isinstance(rval, tuple):
                parameter_json = json.dumps(tuple(parameter_value))
            else:
                parameter_json = json.dumps(parameter_value)
            parameters[k] = parameter_json

        elif isinstance(rval, OpenMLFlow):

            # A subcomponent, for example the base model in
            # AdaBoostClassifier
            sub_components[k] = rval
            sub_components_explicit.add(k)
            component_reference = OrderedDict()
            component_reference['oml-python:serialized_object'] = 'component_reference'
            cr_value = OrderedDict()
            cr_value['key'] = k
            cr_value['step_name'] = None
            component_reference['value'] = cr_value
            component_reference = sklearn_to_flow(component_reference, model)
            parameters[k] = json.dumps(component_reference)

        else:
            # a regular hyperparameter
            if not (hasattr(rval, '__len__') and len(rval) == 0):
                rval = json.dumps(rval)
                parameters[k] = rval
            else:
                parameters[k] = None

        parameters_meta_info[k] = OrderedDict((('description', None), ('data_type', None)))

    return parameters, parameters_meta_info, sub_components, sub_components_explicit


def _get_fn_arguments_with_defaults(fn_name: Callable) -> Tuple[Dict, Set]:
    """
    Returns:
        i) a dict with all parameter names that have a default value, and
        ii) a set with all parameter names that do not have a default

    Parameters
    ----------
    fn_name : callable
        The function of which we want to obtain the defaults

    Returns
    -------
    params_with_defaults: dict
        a dict mapping parameter name to the default value
    params_without_defaults: set
        a set with all parameters that do not have a default value
    """
    # parameters with defaults are optional, all others are required.
    signature = inspect.getfullargspec(fn_name)
    if signature.defaults:
        optional_params = dict(zip(reversed(signature.args), reversed(signature.defaults)))
    else:
        optional_params = dict()
    required_params = {arg for arg in signature.args if arg not in optional_params}
    return optional_params, required_params


def _deserialize_model(
    flow: OpenMLFlow,
    keep_defaults: bool,
    recursion_depth: int,
) -> Any:
    logging.info('-%s deserialize %s' % ('-' * recursion_depth, flow.name))
    model_name = flow.class_name
    _check_dependencies(flow.dependencies)

    parameters = flow.parameters
    components = flow.components
    parameter_dict = OrderedDict()  # type: Dict[str, Any]

    # Do a shallow copy of the components dictionary so we can remove the
    # components from this copy once we added them into the pipeline. This
    # allows us to not consider them any more when looping over the
    # components, but keeping the dictionary of components untouched in the
    # original components dictionary.
    components_ = copy.copy(components)

    for name in parameters:
        value = parameters.get(name)
        logging.info('--%s flow_parameter=%s, value=%s' %
                     ('-' * recursion_depth, name, value))
        rval = flow_to_sklearn(value,
                               components=components_,
                               initialize_with_defaults=keep_defaults,
                               recursion_depth=recursion_depth + 1)
        parameter_dict[name] = rval

    for name in components:
        if name in parameter_dict:
            continue
        if name not in components_:
            continue
        value = components[name]
        logging.info('--%s flow_component=%s, value=%s'
                     % ('-' * recursion_depth, name, value))
        rval = flow_to_sklearn(value,
                               recursion_depth=recursion_depth + 1)
        parameter_dict[name] = rval

    module_name = model_name.rsplit('.', 1)
    model_class = getattr(importlib.import_module(module_name[0]),
                          module_name[1])

    if keep_defaults:
        # obtain all params with a default
        param_defaults, _ =\
            _get_fn_arguments_with_defaults(model_class.__init__)

        # delete the params that have a default from the dict,
        # so they get initialized with their default value
        # except [...]
        for param in param_defaults:
            # [...] the ones that also have a key in the components dict.
            # As OpenML stores different flows for ensembles with different
            # (base-)components, in OpenML terms, these are not considered
            # hyperparameters but rather constants (i.e., changing them would
            # result in a different flow)
            if param not in components.keys():
                del parameter_dict[param]
    return model_class(**parameter_dict)


def _check_dependencies(dependencies: str) -> None:
    if not dependencies:
        return

    dependencies_list = dependencies.split('\n')
    for dependency_string in dependencies_list:
        match = DEPENDENCIES_PATTERN.match(dependency_string)
        if not match:
            raise ValueError('Cannot parse dependency %s' % dependency_string)

        dependency_name = match.group('name')
        operation = match.group('operation')
        version = match.group('version')

        module = importlib.import_module(dependency_name)
        required_version = LooseVersion(version)
        installed_version = LooseVersion(module.__version__)  # type: ignore

        if operation == '==':
            check = required_version == installed_version
        elif operation == '>':
            check = installed_version > required_version
        elif operation == '>=':
            check = (installed_version > required_version
                     or installed_version == required_version)
        else:
            raise NotImplementedError(
                'operation \'%s\' is not supported' % operation)
        if not check:
            raise ValueError('Trying to deserialize a model with dependency '
                             '%s not satisfied.' % dependency_string)


def serialize_type(o: Any) -> 'OrderedDict[str, str]':
    mapping = {float: 'float',
               np.float: 'np.float',
               np.float32: 'np.float32',
               np.float64: 'np.float64',
               int: 'int',
               np.int: 'np.int',
               np.int32: 'np.int32',
               np.int64: 'np.int64'}
    ret = OrderedDict()  # type: 'OrderedDict[str, str]'
    ret['oml-python:serialized_object'] = 'type'
    ret['value'] = mapping[o]
    return ret


def deserialize_type(o: str) -> Any:
    mapping = {'float': float,
               'np.float': np.float,
               'np.float32': np.float32,
               'np.float64': np.float64,
               'int': int,
               'np.int': np.int,
               'np.int32': np.int32,
               'np.int64': np.int64}
    return mapping[o]


def serialize_rv_frozen(o: Any) -> 'OrderedDict[str, Union[str, Dict]]':
    args = o.args
    kwds = o.kwds
    a = o.a
    b = o.b
    dist = o.dist.__class__.__module__ + '.' + o.dist.__class__.__name__
    ret = OrderedDict()  # type: 'OrderedDict[str, Union[str, Dict]]'
    ret['oml-python:serialized_object'] = 'rv_frozen'
    ret['value'] = OrderedDict((('dist', dist), ('a', a), ('b', b),
                                ('args', args), ('kwds', kwds)))
    return ret


def deserialize_rv_frozen(o: 'OrderedDict[str, str]') -> Any:
    args = o['args']
    kwds = o['kwds']
    a = o['a']
    b = o['b']
    dist_name = o['dist']

    module_name = dist_name.rsplit('.', 1)
    try:
        rv_class = getattr(importlib.import_module(module_name[0]),
                           module_name[1])
    except AttributeError:
        warnings.warn('Cannot create model %s for flow.' % dist_name)
        return None

    dist = scipy.stats.distributions.rv_frozen(rv_class(), *args, **kwds)
    dist.a = a
    dist.b = b

    return dist


def serialize_function(o: Callable) -> 'OrderedDict[str, str]':
    name = o.__module__ + '.' + o.__name__
    ret = OrderedDict()  # type: 'OrderedDict[str, str]'
    ret['oml-python:serialized_object'] = 'function'
    ret['value'] = name
    return ret


def deserialize_function(name: str) -> Callable:
    module_name = name.rsplit('.', 1)
    function_handle = getattr(importlib.import_module(module_name[0]), module_name[1])
    return function_handle


def _serialize_cross_validator(o: Any) -> 'OrderedDict[str, Union[str, Dict]]':
    ret = OrderedDict()  # type: 'OrderedDict[str, Union[str, Dict]]'

    parameters = OrderedDict()  # type: 'OrderedDict[str, Any]'

    # XXX this is copied from sklearn.model_selection._split
    cls = o.__class__
    init = getattr(cls.__init__, 'deprecated_original', cls.__init__)
    # Ignore varargs, kw and default values and pop self
    init_signature = signature(init)
    # Consider the constructor parameters excluding 'self'
    if init is object.__init__:
        args = []  # type: List
    else:
        args = sorted([p.name for p in init_signature.parameters.values()
                       if p.name != 'self' and p.kind != p.VAR_KEYWORD])

    for key in args:
        # We need deprecation warnings to always be on in order to
        # catch deprecated param values.
        # This is set in utils/__init__.py but it gets overwritten
        # when running under python3 somehow.
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", DeprecationWarning)
            value = getattr(o, key, None)
            if w is not None and len(w) and w[0].category == DeprecationWarning:
                # if the parameter is deprecated, don't show it
                continue

        if not (hasattr(value, '__len__') and len(value) == 0):
            value = json.dumps(value)
            parameters[key] = value
        else:
            parameters[key] = None

    ret['oml-python:serialized_object'] = 'cv_object'
    name = o.__module__ + "." + o.__class__.__name__
    value = OrderedDict([('name', name), ('parameters', parameters)])
    ret['value'] = value

    return ret


def _deserialize_cross_validator(value: 'OrderedDict[str, Any]', recursion_depth: int) -> Any:
    model_name = value['name']
    parameters = value['parameters']

    module_name = model_name.rsplit('.', 1)
    model_class = getattr(importlib.import_module(module_name[0]),
                          module_name[1])
    for parameter in parameters:
        parameters[parameter] = flow_to_sklearn(
            parameters[parameter], recursion_depth=recursion_depth + 1
        )
    return model_class(**parameters)


def _format_external_version(model_package_name: str, model_package_version_number: str) -> str:
    return '%s==%s' % (model_package_name, model_package_version_number)


# This can possibly be done by a package such as pyxb, but I could not get
# it to work properly.
def get_version_information() -> List[str]:
    """Gets versions of python, sklearn, numpy and scipy, returns them in an
    array,

    Returns
    -------
    result : an array with version information of the above packages
    """
    import sklearn
    import scipy
    import numpy

    major, minor, micro, _, _ = sys.version_info
    python_version = 'Python_{}.'.format(
        ".".join([str(major), str(minor), str(micro)]))
    sklearn_version = 'Sklearn_{}.'.format(sklearn.__version__)
    numpy_version = 'NumPy_{}.'.format(numpy.__version__)
    scipy_version = 'SciPy_{}.'.format(scipy.__version__)

    return [python_version, sklearn_version, numpy_version, scipy_version]


def check_n_jobs(model: Any) -> bool:
    def check(param_grid, restricted_parameter_name, legal_values):
        if isinstance(param_grid, dict):
            for param, value in param_grid.items():
                # n_jobs is scikitlearn parameter for paralizing jobs
                if param.split('__')[-1] == restricted_parameter_name:
                    # 0 = illegal value (?), 1 / None = use one core,
                    # n = use n cores,
                    # -1 = use all available cores -> this makes it hard to
                    # measure runtime in a fair way
                    if legal_values is None or value not in legal_values:
                        return False
            return True
        elif isinstance(param_grid, list):
            return all(
                check(sub_grid, restricted_parameter_name, legal_values)
                for sub_grid in param_grid
            )

    if not (
        isinstance(model, sklearn.base.BaseEstimator)
        or isinstance(model, sklearn.model_selection._search.BaseSearchCV)
    ):
        raise ValueError('model should be BaseEstimator or BaseSearchCV')

    # make sure that n_jobs is not in the parameter grid of optimization
    # procedure
    if isinstance(model, sklearn.model_selection._search.BaseSearchCV):
        if isinstance(model, sklearn.model_selection.GridSearchCV):
            param_distributions = model.param_grid
        elif isinstance(model, sklearn.model_selection.RandomizedSearchCV):
            param_distributions = model.param_distributions
        else:
            if hasattr(model, 'param_distributions'):
                param_distributions = model.param_distributions
            else:
                raise AttributeError('Using subclass BaseSearchCV other than '
                                     '{GridSearchCV, RandomizedSearchCV}. '
                                     'Could not find attribute '
                                     'param_distributions.')
            print('Warning! Using subclass BaseSearchCV other than '
                  '{GridSearchCV, RandomizedSearchCV}. '
                  'Should implement param check. ')

        if not check(param_distributions, 'n_jobs', None):
            raise PyOpenMLError('openml-python should not be used to '
                                'optimize the n_jobs parameter.')

    # check the parameters for n_jobs
    return check(model.get_params(), 'n_jobs', [1, None])
