from collections import OrderedDict
import json
import time
from typing import Any, List, Tuple
import warnings

import numpy as np
import sklearn.model_selection

from openml.tasks import (
    OpenMLSupervisedTask,
    TaskTypeEnum,
    OpenMLTask,
    OpenMLClassificationTask,
    OpenMLLearningCurveTask,
)
from openml.exceptions import PyOpenMLError
from openml.extensions import Extension
from openml.flows.sklearn_converter import (
    sklearn_to_flow,
    flow_to_sklearn,
    obtain_parameter_values,
)
from openml.runs.trace import OpenMLRunTrace, PREFIX


class SklearnExtension(Extension):

    def flow_to_model(self, flow):
        return flow_to_sklearn(flow)

    def model_to_flow(self, model):
        return sklearn_to_flow(model)

    def flow_to_parameters(self, flow):
        return obtain_parameter_values(flow)

    def is_estimator(self, model):
        return hasattr(model, 'fit') and hasattr(model, 'predict')

    def seed_model(self, model, seed=None):
        """Sets all the non-seeded components of a model with a seed.
           Models that are already seeded will maintain the seed. In
           this case, only integer seeds are allowed (An exception
           is thrown when a RandomState was used as seed)

            Parameters
            ----------
            model : sklearn model
                The model to be seeded
            seed : int
                The seed to initialize the RandomState with. Unseeded subcomponents
                will be seeded with a random number from the RandomState.

            Returns
            -------
            model : sklearn model
                a version of the model where all (sub)components have
                a seed
        """

        def _seed_current_object(current_value):
            if isinstance(current_value, int):  # acceptable behaviour
                return False
            elif isinstance(current_value, np.random.RandomState):
                raise ValueError(
                    'Models initialized with a RandomState object are not '
                    'supported. Please seed with an integer. ')
            elif current_value is not None:
                raise ValueError(
                    'Models should be seeded with int or None (this should never '
                    'happen). ')
            else:
                return True

        rs = np.random.RandomState(seed)
        model_params = model.get_params()
        random_states = {}
        for param_name in sorted(model_params):
            if 'random_state' in param_name:
                current_value = model_params[param_name]
                # important to draw the value at this point (and not in the if
                # statement) this way we guarantee that if a different set of
                # subflows is seeded, the same number of the random generator is
                # used
                new_value = rs.randint(0, 2 ** 16)
                if _seed_current_object(current_value):
                    random_states[param_name] = new_value

            # Also seed CV objects!
            elif isinstance(model_params[param_name],
                            sklearn.model_selection.BaseCrossValidator):
                if not hasattr(model_params[param_name], 'random_state'):
                    continue

                current_value = model_params[param_name].random_state
                new_value = rs.randint(0, 2 ** 16)
                if _seed_current_object(current_value):
                    model_params[param_name].random_state = new_value

        model.set_params(**random_states)
        return model

    def _run_model_on_fold(
        self,
        model: Any,
        task: OpenMLTask,
        rep_no: int,
        fold_no: int,
        sample_no: int,
        can_measure_runtime: bool,
        add_local_measures: bool,
        extension: Extension,
    ) -> Tuple:
        """Internal function that executes a model on a fold (and possibly
           subsample) of the dataset. It returns the data that is necessary
           to construct the OpenML Run object (potentially over more than
           one folds). Is used by run_task_get_arff_content. Do not use this
           function unless you know what you are doing.

            Parameters
            ----------
            model : sklearn model
                The UNTRAINED model to run
            task : OpenMLTask
                The task to run the model on
            rep_no : int
                The repeat of the experiment (0-based; in case of 1 time CV,
                always 0)
            fold_no : int
                The fold nr of the experiment (0-based; in case of holdout,
                always 0)
            sample_no : int
                In case of learning curves, the index of the subsample (0-based;
                in case of no learning curve, always 0)
            can_measure_runtime : bool
                Whether we are allowed to measure runtime (requires: Single node
                computation and Python >= 3.3)
            add_local_measures : bool
                Determines whether to calculate a set of measures (i.e., predictive
                accuracy) locally, to later verify server behaviour
            extension : openml.extensions.Extension
                BLABLABLA

            Returns
            -------
            arff_datacontent : List[List]
                Arff representation (list of lists) of the predictions that were
                generated by this fold (for putting in predictions.arff)
            arff_tracecontent :  List[List]
                Arff representation (list of lists) of the trace data that was
                generated by this fold (for putting in trace.arff)
            user_defined_measures : Dict[float]
                User defined measures that were generated on this fold
            model : sklearn model
                The model trained on this fold
        """

        def _prediction_to_probabilities(
                y: np.ndarray,
                model_classes: List,
        ) -> np.ndarray:
            """Transforms predicted probabilities to match with OpenML class indices.

            Parameters
            ----------
            y : np.ndarray
                Predicted probabilities (possibly omitting classes if they were not present in the
                training data).
            model_classes : list
                List of classes known_predicted by the model, ordered by their index.

            Returns
            -------
            np.ndarray
            """
            # y: list or numpy array of predictions
            # model_classes: sklearn classifier mapping from original array id to
            # prediction index id
            if not isinstance(model_classes, list):
                raise ValueError('please convert model classes to list prior to '
                                 'calling this fn')
            result = np.zeros((len(y), len(model_classes)), dtype=np.float32)
            for obs, prediction_idx in enumerate(y):
                array_idx = model_classes.index(prediction_idx)
                result[obs][array_idx] = 1.0
            return result

        # TODO: if possible, give a warning if model is already fitted (acceptable
        # in case of custom experimentation,
        # but not desirable if we want to upload to OpenML).

        model_copy = sklearn.base.clone(model, safe=True)

        train_indices, test_indices = task.get_train_test_split_indices(
            repeat=rep_no, fold=fold_no, sample=sample_no)
        if isinstance(task, OpenMLSupervisedTask):
            x, y = task.get_X_and_y()
            train_x = x[train_indices]
            train_y = y[train_indices]
            test_x = x[test_indices]
            test_y = y[test_indices]
        elif task.task_type_id in (
                TaskTypeEnum.CLUSTERING,
        ):
            train_x = train_indices
            test_x = test_indices
        else:
            raise NotImplementedError(task.task_type)

        user_defined_measures = OrderedDict()  # type: 'OrderedDict[str, float]'

        try:
            # for measuring runtime. Only available since Python 3.3
            if can_measure_runtime:
                modelfit_starttime = time.process_time()

            if task.task_type_id in (
                    TaskTypeEnum.SUPERVISED_CLASSIFICATION,
                    TaskTypeEnum.SUPERVISED_REGRESSION,
                    TaskTypeEnum.LEARNING_CURVE,
            ):
                model_copy.fit(train_x, train_y)
            elif task.task_type in (
                    TaskTypeEnum.CLUSTERING,
            ):
                model_copy.fit(train_x)

            if can_measure_runtime:
                modelfit_duration = \
                    (time.process_time() - modelfit_starttime) * 1000
                user_defined_measures['usercpu_time_millis_training'] = \
                    modelfit_duration
        except AttributeError as e:
            # typically happens when training a regressor on classification task
            raise PyOpenMLError(str(e))

        # extract trace, if applicable
        arff_tracecontent = []  # type: List[List]
        if extension.is_hpo_class(model_copy):
            arff_tracecontent.extend(extension.extract_trace_data(model_copy, rep_no, fold_no))

        if task.task_type_id in (
                TaskTypeEnum.SUPERVISED_CLASSIFICATION,
                TaskTypeEnum.LEARNING_CURVE,
        ):
            # search for model classes_ (might differ depending on modeltype)
            # first, pipelines are a special case (these don't have a classes_
            # object, but rather borrows it from the last step. We do this manually,
            # because of the BaseSearch check)
            if isinstance(model_copy, sklearn.pipeline.Pipeline):
                used_estimator = model_copy.steps[-1][-1]
            else:
                used_estimator = model_copy

            if isinstance(used_estimator,
                          sklearn.model_selection._search.BaseSearchCV):
                model_classes = used_estimator.best_estimator_.classes_
            else:
                model_classes = used_estimator.classes_

        if can_measure_runtime:
            modelpredict_starttime = time.process_time()

        # In supervised learning this returns the predictions for Y, in clustering
        # it returns the clusters
        pred_y = model_copy.predict(test_x)

        if can_measure_runtime:
            modelpredict_duration = \
                (time.process_time() - modelpredict_starttime) * 1000
            user_defined_measures['usercpu_time_millis_testing'] = \
                modelpredict_duration
            user_defined_measures['usercpu_time_millis'] = \
                modelfit_duration + modelpredict_duration

        # add client-side calculated metrics. These is used on the server as
        # consistency check, only useful for supervised tasks
        def _calculate_local_measure(sklearn_fn, openml_name):
            user_defined_measures[openml_name] = sklearn_fn(test_y, pred_y)

        # Task type specific outputs
        arff_datacontent = []

        if isinstance(task, (OpenMLClassificationTask, OpenMLLearningCurveTask)):

            try:
                proba_y = model_copy.predict_proba(test_x)
            except AttributeError:
                proba_y = _prediction_to_probabilities(pred_y, list(model_classes))

            if proba_y.shape[1] != len(task.class_labels):
                warnings.warn("Repeat %d Fold %d: estimator only predicted for "
                              "%d/%d classes!" % (
                                  rep_no, fold_no, proba_y.shape[1],
                                  len(task.class_labels)))

            if add_local_measures:
                _calculate_local_measure(sklearn.metrics.accuracy_score,
                                         'predictive_accuracy')

            for i in range(0, len(test_indices)):
                arff_line = self._prediction_to_row(rep_no, fold_no, sample_no,
                                                    test_indices[i],
                                                    task.class_labels[test_y[i]],
                                                    pred_y[i], proba_y[i],
                                                    task.class_labels, model_classes,
                                                    )
                arff_datacontent.append(arff_line)

        elif task.task_type_id == TaskTypeEnum.SUPERVISED_REGRESSION:
            if add_local_measures:
                _calculate_local_measure(sklearn.metrics.mean_absolute_error,
                                         'mean_absolute_error')

            for i in range(0, len(test_indices)):
                arff_line = [rep_no, fold_no, test_indices[i], pred_y[i],
                             test_y[i]]
                arff_datacontent.append(arff_line)

        elif task.task_type_id == TaskTypeEnum.CLUSTERING:
            for i in range(0, len(test_indices)):
                arff_line = [test_indices[i], pred_y[i]]  # row_id, cluster ID
                arff_datacontent.append(arff_line)

        return arff_datacontent, arff_tracecontent, user_defined_measures, model_copy

    def _prediction_to_row(self, rep_no, fold_no, sample_no, row_id, correct_label,
                           predicted_label, predicted_probabilities, class_labels,
                           model_classes_mapping):
        """Util function that turns probability estimates of a classifier for a
        given instance into the right arff format to upload to openml.

            Parameters
            ----------
            rep_no : int
                The repeat of the experiment (0-based; in case of 1 time CV,
                always 0)
            fold_no : int
                The fold nr of the experiment (0-based; in case of holdout,
                always 0)
            sample_no : int
                In case of learning curves, the index of the subsample (0-based;
                in case of no learning curve, always 0)
            row_id : int
                row id in the initial dataset
            correct_label : str
                original label of the instance
            predicted_label : str
                the label that was predicted
            predicted_probabilities : array (size=num_classes)
                probabilities per class
            class_labels : array (size=num_classes)
            model_classes_mapping : list
                A list of classes the model produced.
                Obtained by BaseEstimator.classes_

            Returns
            -------
            arff_line : list
                representation of the current prediction in OpenML format
            """
        if not isinstance(rep_no, (int, np.integer)):
            raise ValueError('rep_no should be int')
        if not isinstance(fold_no, (int, np.integer)):
            raise ValueError('fold_no should be int')
        if not isinstance(sample_no, (int, np.integer)):
            raise ValueError('sample_no should be int')
        if not isinstance(row_id, (int, np.integer)):
            raise ValueError('row_id should be int')
        if not len(predicted_probabilities) == len(model_classes_mapping):
            raise ValueError('len(predicted_probabilities) != len(class_labels)')

        arff_line = [rep_no, fold_no, sample_no, row_id]
        for class_label_idx in range(len(class_labels)):
            if class_label_idx in model_classes_mapping:
                index = np.where(model_classes_mapping == class_label_idx)[0][0]
                # TODO: WHY IS THIS 2D???
                arff_line.append(predicted_probabilities[index])
            else:
                arff_line.append(0.0)

        arff_line.append(class_labels[predicted_label])
        arff_line.append(correct_label)
        return arff_line

    def is_hpo_class(self, model):
        return isinstance(model, sklearn.model_selection._search.BaseSearchCV)

    def assert_hpo_class(self, model):
        if not self.is_hpo_class(model):
            raise AssertionError(
                'Flow model %s is not an instance of sklearn.model_selection._search.BaseSearchCV'
                % model
            )

    def assert_hpo_class_has_trace(self, model):
        if not hasattr(model, 'cv_results_'):
            raise ValueError('model should contain `cv_results_`')

    def instantiate_model_from_hpo_class(self, model, trace_iteration):
        base_estimator = model.estimator
        base_estimator.set_params(**trace_iteration.get_parameters())
        return base_estimator

    def obtain_arff_trace(self, extension, model, trace_content):
        self.assert_hpo_class(model)
        self.assert_hpo_class_has_trace(model)

        # attributes that will be in trace arff, regardless of the model
        trace_attributes = [('repeat', 'NUMERIC'),
                            ('fold', 'NUMERIC'),
                            ('iteration', 'NUMERIC'),
                            ('evaluation', 'NUMERIC'),
                            ('selected', ['true', 'false'])]

        # model dependent attributes for trace arff
        for key in model.cv_results_:
            if key.startswith('param_'):
                # supported types should include all types, including bool,
                # int float
                supported_basic_types = (bool, int, float, str)
                for param_value in model.cv_results_[key]:
                    if isinstance(param_value, supported_basic_types) or \
                            param_value is None or param_value is np.ma.masked:
                        # basic string values
                        type = 'STRING'
                    elif isinstance(param_value, list) and \
                            all(isinstance(i, int) for i in param_value):
                        # list of integers
                        type = 'STRING'
                    else:
                        raise TypeError('Unsupported param type in param grid: %s' % key)

                # renamed the attribute param to parameter, as this is a required
                # OpenML convention - this also guards against name collisions
                # with the required trace attributes
                attribute = (PREFIX + key[6:], type)
                trace_attributes.append(attribute)

        return OpenMLRunTrace.generate(
            trace_attributes,
            trace_content,
        )

    def extract_trace_data(self, model, rep_no, fold_no):
        arff_tracecontent = []
        for itt_no in range(0, len(model.cv_results_['mean_test_score'])):
            # we use the string values for True and False, as it is defined in
            # this way by the OpenML server
            selected = 'false'
            if itt_no == model.best_index_:
                selected = 'true'
            test_score = model.cv_results_['mean_test_score'][itt_no]
            arff_line = [rep_no, fold_no, itt_no, test_score, selected]
            for key in model.cv_results_:
                if key.startswith('param_'):
                    value = model.cv_results_[key][itt_no]
                    if value is not np.ma.masked:
                        serialized_value = json.dumps(value)
                    else:
                        serialized_value = np.nan
                    arff_line.append(serialized_value)
            arff_tracecontent.append(arff_line)
        return arff_tracecontent
