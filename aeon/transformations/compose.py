"""Meta-transformers for building composite transformers."""

from warnings import warn

import numpy as np
import pandas as pd
from deprecated.sphinx import deprecated
from sklearn import clone

from aeon.base import _HeterogenousMetaEstimator
from aeon.testing.mock_estimators import MockTransformer
from aeon.transformations._legacy._delegate import _DelegatedTransformer
from aeon.transformations.base import BaseTransformer
from aeon.utils.multiindex import flatten_multiindex
from aeon.utils.sklearn import is_sklearn_transformer
from aeon.utils.validation.series import check_series

__maintainer__ = []
__all__ = [
    "ColumnwiseTransformer",
    "ColumnConcatenator",
    "FeatureUnion",
    "FitInTransform",
    "Id",
    "InvertTransform",
    "MultiplexTransformer",
    "OptionalPassthrough",
    "TransformerPipeline",
    "YtoX",
]
from aeon.transformations._legacy._boxcox import _BoxCoxTransformer
from aeon.utils import ALL_TIME_SERIES_TYPES


def _coerce_to_aeon(other):
    """Check and format inputs to dunders for compose."""
    from aeon.transformations._legacy.adapt import TabularToSeriesAdaptor

    # if sklearn transformer, adapt to aeon transformer first
    if is_sklearn_transformer(other):
        return TabularToSeriesAdaptor(other)

    return other


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="TransformerPipeline will be removed in version 0.11.0.",
    category=FutureWarning,
)
class TransformerPipeline(_HeterogenousMetaEstimator, BaseTransformer):
    """
    Pipeline of transformers compositor.

    The `TransformerPipeline` compositor allows to chain transformers.
    The pipeline is constructed with a list of aeon transformers, i.e.
    estimators following the BaseTransformer interface. The list can be
    unnamed (a simple list of transformers) or string named (a list of
    pairs of string, estimator).

    For a list of transformers `trafo1`, `trafo2`, ..., `trafoN`,
    the pipeline behaves as follows:

    * `fit`
        Changes state by running `trafo1.fit_transform`,
        trafo2.fit_transform` etc sequentially, with
        `trafo[i]` receiving the output of `trafo[i-1]`
    * `transform`
        Result is of executing `trafo1.transform`, `trafo2.transform`,
        etc with `trafo[i].transform` input = output of `trafo[i-1].transform`,
        and returning the output of `trafoN.transform`
    * `inverse_transform`
        Result is of executing `trafo[i].inverse_transform`,
        with `trafo[i].inverse_transform` input = output
        `trafo[i-1].inverse_transform`, and returning the output of
        `trafoN.inverse_transform`
    * `update`
        Changes state by chaining `trafo1.update`, `trafo1.transform`,
        `trafo2.update`, `trafo2.transform`, ..., `trafoN.update`,
        where `trafo[i].update` and `trafo[i].transform` receive as input
        the output of `trafo[i-1].transform`

    The `get_params`, `set_params` uses `sklearn` compatible nesting interface
    if list is unnamed, names are generated as names of classes
    if names are non-unique, `f"_{str(i)}"` is appended to each name string
    where `i` is the total count of occurrence of a non-unique string
    inside the list of names leading up to it (inclusive)

    A `TransformerPipeline` can also be created by using the magic multiplication
    on any transformer, i.e., any estimator inheriting from `BaseTransformer`
    for instance, `my_trafo1 * my_trafo2 * my_trafo3`
    will result in the same object as  obtained from the constructor
    `TransformerPipeline([my_trafo1, my_trafo2, my_trafo3])`
    A magic multiplication can also be used with (str, transformer) pairs,
    as long as one element in the chain is a transformer

    Parameters
    ----------
    steps : list of aeon transformers, or
        List of tuples (str, transformer) of aeon transformers
        these are "blueprint" transformers, states do not change when `fit` is called.

    Attributes
    ----------
    steps_ : list of tuples (str, transformer) of aeon transformers
        Clones of transformers in `steps` which are fitted in the pipeline
        is always in (str, transformer) format, even if `steps` is just a list
        strings not passed in `steps` are replaced by unique generated strings
        i-th transformer in `steps_` is clone of i-th in `steps`.
    """

    _tags = {
        # we let all X inputs through to be handled by first transformer
        "X_inner_type": ALL_TIME_SERIES_TYPES,
        "capability:multivariate": True,
    }

    # no further default tag values - these are set dynamically below

    # for default get_params/set_params from _HeterogenousMetaEstimator
    # _steps_attr points to the attribute of self
    # which contains the heterogeneous set of estimators
    # this must be an iterable of (name: str, estimator, ...) tuples for the default
    _steps_attr = "_steps"
    # if the estimator is fittable, _HeterogenousMetaEstimator also
    # provides an override for get_fitted_params for params from the fitted estimators
    # the fitted estimators should be in a different attribute, _steps_fitted_attr
    # this must be an iterable of (name: str, estimator, ...) tuples for the default
    _steps_fitted_attr = "steps_"

    def __init__(self, steps):
        self.steps = steps
        self.steps_ = self._check_estimators(self.steps, cls_type=BaseTransformer)

        super().__init__()

        # abbreviate for readability
        ests = self.steps_
        first_trafo = ests[0][1]
        last_trafo = ests[-1][1]

        self.clone_tags(first_trafo, ["input_data_type"])
        # output type is that of last estimator, if no "Primitives" occur in the middle
        # if "Primitives" occur in the middle, then output is set to that too
        # this is in a case where "Series-to-Series" is applied to primitive df
        #   e.g., in a case of pipelining with scikit-learn transformers
        last_out = last_trafo.get_tag("output_data_type")
        self._anytagis_then_set("output_data_type", "Primitives", last_out, ests)

        # set property tags based on tags of components
        self._anytag_notnone_set("y_inner_type", ests)
        self._anytag_notnone_set("transform_labels", ests)

        self._anytagis_then_set("instancewise", False, True, ests)
        self._anytagis_then_set("fit_is_empty", False, True, ests)
        self._anytagis_then_set("transform-returns-same-time-index", False, True, ests)
        self._anytagis_then_set("skip-inverse-transform", False, True, ests)

        # self can inverse transform if for all est, we either skip or can inv-trasform
        skips = [est.get_tag("skip-inverse-transform") for _, est in ests]
        has_invs = [est.get_tag("capability:inverse_transform") for _, est in ests]
        can_inv = [x or y for x, y in zip(skips, has_invs)]
        self.set_tags(**{"capability:inverse_transform": all(can_inv)})

        # can handle missing data iff all estimators can handle missing data
        #   up to a potential estimator when missing data is removed
        # removes missing data iff can handle missing data,
        #   and there is an estimator in the chain that removes it
        self._tagchain_is_linked_set(
            "capability:missing_values", "capability:missing_values:removes", ests
        )
        # can handle unequal length iff all estimators can handle unequal length
        #   up to a potential estimator which turns the series equal length
        # removes unequal length iff can handle unequal length,
        #   and there is an estimator in the chain that renders series equal length
        self._tagchain_is_linked_set(
            "capability:unequal_length", "capability:unequal_length:removes", ests
        )

    @property
    def _steps(self):
        return self._get_estimator_tuples(self.steps, clone_ests=False)

    @_steps.setter
    def _steps(self, value):
        self.steps = value

    def __mul__(self, other):
        """Magic * method, return (right) concatenated TransformerPipeline.

        Implemented for `other` being a transformer, otherwise returns `NotImplemented`.

        Parameters
        ----------
        other: `aeon` transformer, must inherit from BaseTransformer
            otherwise, `NotImplemented` is returned

        Returns
        -------
        TransformerPipeline object, concatenation of `self` (first) with `other` (last).
            not nested, contains only non-TransformerPipeline `aeon` transformers
        """
        other = _coerce_to_aeon(other)
        return self._dunder_concat(
            other=other,
            base_class=BaseTransformer,
            composite_class=TransformerPipeline,
            attr_name="steps",
            concat_order="left",
        )

    def __rmul__(self, other):
        """Magic * method, return (left) concatenated TransformerPipeline.

        Implemented for `other` being a transformer, otherwise returns `NotImplemented`.

        Parameters
        ----------
        other: `aeon` transformer, must inherit from BaseTransformer
            otherwise, `NotImplemented` is returned

        Returns
        -------
        TransformerPipeline object, concatenation of `other` (first) with `self` (last).
            not nested, contains only non-TransformerPipeline `aeon` steps
        """
        other = _coerce_to_aeon(other)
        return self._dunder_concat(
            other=other,
            base_class=BaseTransformer,
            composite_class=TransformerPipeline,
            attr_name="steps",
            concat_order="right",
        )

    def _fit(self, X, y=None):
        """Fit transformer to X and y.

        private _fit containing the core logic, called from fit

        Parameters
        ----------
        X: data structure of type X_inner_type
            if X_inner_type is list, _fit must support all types in it
            Data to fit transform to
        y : Series or Panel of type y_inner_type, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        self: reference to self
        """
        self.steps_ = self._check_estimators(self.steps, cls_type=BaseTransformer)

        Xt = X
        for _, transformer in self.steps_:
            Xt = transformer.fit_transform(X=Xt, y=y)

        return self

    def _transform(self, X, y=None):
        """Transform X and return a transformed version.

        private _transform containing core logic, called from transform

        Parameters
        ----------
        X: data structure of type X_inner_type
            if X_inner_type is list, _transform must support all types in it
            Data to be transformed
        y : Series or Panel of type y_inner_type, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        transformed version of X
        """
        Xt = X
        for _, transformer in self.steps_:
            if not self.get_tag("fit_is_empty", False):
                Xt = transformer.transform(X=Xt, y=y)
            else:
                Xt = transformer.fit_transform(X=Xt, y=y)

        return Xt

    def _inverse_transform(self, X, y=None):
        """Inverse transform, inverse operation to transform.

        private _inverse_transform containing core logic, called from inverse_transform

        Parameters
        ----------
        X: data structure of type X_inner_type
            if X_inner_type is list, _inverse_transform must support all types in it
            Data to be inverse transformed
        y : Series or Panel of type y_inner_type, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        inverse transformed version of X
        """
        Xt = X
        for _, transformer in reversed(self.steps_):
            if not self.get_tag("fit_is_empty", False):
                Xt = transformer.inverse_transform(X=Xt, y=y)
            else:
                Xt = transformer.fit(X=Xt, y=y).inverse_transform(X=Xt, y=y)

        return Xt

    def _update(self, X, y=None):
        """Update transformer with X and y.

        private _update containing the core logic, called from update

        Parameters
        ----------
        X: data structure of type X_inner_type
            if X_inner_type is list, _update must support all types in it
            Data to update transformer with
        y : Series or Panel of type y_inner_type, default=None
            Additional data, e.g., labels for tarnsformation

        Returns
        -------
        self: reference to self
        """
        Xt = X
        for _, transformer in self.steps_:
            transformer.update(X=Xt, y=y)
            Xt = transformer.transform(X=Xt, y=y)

        return self

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.

        Returns
        -------
        params : dict or list of dict, default={}
            Parameters to create testing instances of the class.
            Each dict are parameters to construct an "interesting" test instance, i.e.,
            `MyClass(**params)` or `MyClass(**params[i])` creates a valid test instance.
            `create_test_instance` uses the first (or only) dictionary in `params`.
        """
        # imports
        from aeon.testing.mock_estimators import MockTransformer

        t1 = MockTransformer(power=2)
        t2 = MockTransformer(power=0.5)
        t3 = MockTransformer(power=1)

        # construct without names
        params1 = {"steps": [t1, t2]}

        # construct with names
        params2 = {"steps": [("foo", t1), ("bar", t2), ("foobar", t3)]}

        # construct with names and provoke multiple naming clashes
        params3 = {"steps": [("foo", t1), ("foo", t2), ("foo_1", t3)]}

        return [params1, params2, params3]


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="FeatureUnion will be removed in version 0.11.0.",
    category=FutureWarning,
)
class FeatureUnion(_HeterogenousMetaEstimator, BaseTransformer):
    """Concatenates results of multiple transformer objects.

    This estimator applies a list of transformer objects in parallel to the
    input data, then concatenates the results. This is useful to combine
    several feature extraction mechanisms into a single transformer.
    Parameters of the transformations may be set using its name and the
    parameter name separated by a '__'. A transformer may be replaced entirely by
    setting the parameter with its name to another transformer,
    or removed by setting to 'drop' or ``None``.

    Parameters
    ----------
    transformer_list : list of (string, transformer) tuples
        List of transformer objects to be applied to the data. The first
        half of each tuple is the name of the transformer.
    n_jobs : int or None, default=None
        Number of jobs to run in parallel.
        ``None`` means 1 unless in a :obj:`joblib.parallel_backend`
        context.
        ``-1`` means using all processors.
    transformer_weights : dict, optional
        Multiplicative weights for features per transformer.
        Keys are transformer names, values the weights.
    flatten_transform_index : bool, default=True
        if True, columns of return DataFrame are flat, by "transformer__variablename"
        if False, columns are MultiIndex (transformer, variablename)
        has no effect if return type is one without column names
    """

    _tags = {
        "input_data_type": "Series",
        "output_data_type": "Series",
        "transform_labels": "None",
        "instancewise": False,  # depends on components
        "capability:multivariate": True,  # depends on components
        "capability:missing_values": False,  # depends on components
        "X_inner_type": ["pd.DataFrame", "pd-multiindex", "pd_multiindex_hier"],
        "y_inner_type": "None",
        "X-y-must-have-same-index": False,
        "enforce_index_type": None,
        "fit_is_empty": False,
        "transform-returns-same-time-index": False,
        "skip-inverse-transform": False,
        "capability:inverse_transform": False,
        # unclear what inverse transform should be, since multiple inverse_transform
        #   would have to inverse transform to one
    }

    # for default get_params/set_params from _HeterogenousMetaEstimator
    # _steps_attr points to the attribute of self
    # which contains the heterogeneous set of estimators
    # this must be an iterable of (name: str, estimator) pairs for the default
    _steps_attr = "_transformer_list"
    # if the estimator is fittable, _HeterogenousMetaEstimator also
    # provides an override for get_fitted_params for params from the fitted estimators
    # the fitted estimators should be in a different attribute, _steps_fitted_attr
    _steps_fitted_attr = "transformer_list_"

    def __init__(
        self,
        transformer_list,
        n_jobs=None,
        transformer_weights=None,
        flatten_transform_index=True,
    ):
        self.transformer_list = transformer_list
        self.transformer_list_ = self._check_estimators(
            transformer_list, cls_type=BaseTransformer
        )

        self.n_jobs = n_jobs
        self.transformer_weights = transformer_weights
        self.flatten_transform_index = flatten_transform_index

        super().__init__()

        # abbreviate for readability
        ests = self.transformer_list_

        # set property tags based on tags of components
        self._anytag_notnone_set("y_inner_type", ests)
        self._anytag_notnone_set("transform_labels", ests)

        self._anytagis_then_set("instancewise", False, True, ests)
        self._anytagis_then_set("X-y-must-have-same-index", True, False, ests)
        self._anytagis_then_set("fit_is_empty", False, True, ests)
        self._anytagis_then_set("transform-returns-same-time-index", False, True, ests)
        self._anytagis_then_set("skip-inverse-transform", True, False, ests)
        # self._anytagis_then_set("capability:inverse_transform", False, True, ests)
        self._anytagis_then_set("capability:missing_values", False, True, ests)
        self._anytagis_then_set("capability:multivariate", False, True, ests)

    @property
    def _transformer_list(self):
        return self._get_estimator_tuples(self.transformer_list, clone_ests=False)

    @_transformer_list.setter
    def _transformer_list(self, value):
        self.transformer_list = value
        self.transformer_list_ = self._check_estimators(value, cls_type=BaseTransformer)

    def __add__(self, other):
        """Magic + method, return (right) concatenated FeatureUnion.

        Implemented for `other` being a transformer, otherwise returns `NotImplemented`.

        Parameters
        ----------
        other: `aeon` transformer, must inherit from BaseTransformer
            otherwise, `NotImplemented` is returned

        Returns
        -------
        TransformerPipeline object, concatenation of `self` (first) with `other` (last).
            not nested, contains only non-FeatureUnion `aeon` transformers
        """
        return self._dunder_concat(
            other=other,
            base_class=BaseTransformer,
            composite_class=FeatureUnion,
            attr_name="transformer_list",
            concat_order="left",
        )

    def __radd__(self, other):
        """Magic + method, return (left) concatenated FeatureUnion.

        Implemented for `other` being a transformer, otherwise returns `NotImplemented`.

        Parameters
        ----------
        other: `aeon` transformer, must inherit from BaseTransformer
            otherwise, `NotImplemented` is returned

        Returns
        -------
        TransformerPipeline object, concatenation of `self` (last) with `other` (first).
            not nested, contains only non-FeatureUnion `aeon` transformers
        """
        return self._dunder_concat(
            other=other,
            base_class=BaseTransformer,
            composite_class=FeatureUnion,
            attr_name="transformer_list",
            concat_order="right",
        )

    def _fit(self, X, y=None):
        """Fit transformer to X and y.

        private _fit containing the core logic, called from fit

        Parameters
        ----------
        X : pd.DataFrame
            Data to fit transform to
        y : Series or Panel of type y_inner_type, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        self: reference to self
        """
        self.transformer_list_ = self._check_estimators(
            self.transformer_list, cls_type=BaseTransformer
        )

        for _, transformer in self.transformer_list_:
            transformer.fit(X=X, y=y)

        return self

    def _transform(self, X, y=None):
        """Transform X and return a transformed version.

        private _transform containing core logic, called from transform

        Parameters
        ----------
        X : pd.DataFrame
            Data to be transformed
        y : Series or Panel of type y_inner_type, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        transformed version of X
        """
        # retrieve fitted transformers, apply to the new data individually
        transformers = self._get_estimator_list(self.transformer_list_)
        if not self.get_tag("fit_is_empty", False):
            Xt_list = [trafo.transform(X, y) for trafo in transformers]
        else:
            Xt_list = [trafo.fit_transform(X, y) for trafo in transformers]

        transformer_names = self._get_estimator_names(self.transformer_list_)

        Xt = pd.concat(
            Xt_list, axis=1, keys=transformer_names, names=["transformer", "variable"]
        )

        if self.flatten_transform_index:
            Xt.columns = flatten_multiindex(Xt.columns)

        return Xt

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Test parameters for FeatureUnion."""
        # with name and estimator tuple, all transformers don't have fit
        TRANSFORMERS = [
            ("transformer1", MockTransformer(power=4)),
            ("transformer2", MockTransformer(power=0.25)),
        ]
        return {"transformer_list": TRANSFORMERS}


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="FitInTransform will be removed in version 0.11.0.",
    category=FutureWarning,
)
class FitInTransform(BaseTransformer):
    """
    Transformer wrapper to delay fit to the transform phase.

    In panel settings, e.g., time series classification, it can be preferable
    (or, necessary) to fit and transform on the test set, e.g., interpolate within the
    same series that interpolation parameters are being fitted on. `FitInTransform` can
    be used to wrap any transformer to ensure that `fit` and `transform` happen always
    on the same series, by delaying the `fit` to the `transform` batch.

    Warning: The use of `FitInTransform` will typically not be useful, or can constitute
    a mistake (data leakage) when naively used in a forecasting setting.

    Parameters
    ----------
    transformer : Estimator
        Scikit-learn-like or aeon-like transformer to fit and apply to series.
    skip_inverse_transform : bool
        The FitInTransform will skip inverse_transform by default, of the param
        skip_inverse_transform=False, then the inverse_transform is calculated
        by means of transformer.fit(X=X, y=y).inverse_transform(X=X, y=y) where
        transformer is the inner transformer. So the inner transformer is
        fitted on the inverse_transform data. This is required to have a non-
        state changing transform() method of FitInTransform.
    """

    def __init__(self, transformer, skip_inverse_transform=True):
        self.transformer = transformer
        self.skip_inverse_transform = skip_inverse_transform
        super().__init__()
        self.clone_tags(transformer, None)
        self.set_tags(
            **{
                "fit_is_empty": True,
                "skip-inverse-transform": self.skip_inverse_transform,
            }
        )

    def _transform(self, X, y=None):
        """Transform X and return a transformed version.

        private _transform containing core logic, called from transform

        Parameters
        ----------
        X: data structure of type X_inner_type
            if X_inner_type is list, _transform must support all types in it
            Data to be transformed
        y : Series or Panel of type y_inner_type, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        transformed version of X
        """
        return clone(self.transformer).fit_transform(X=X, y=y)

    def _inverse_transform(self, X, y=None):
        """Inverse transform, inverse operation to transform.

        private _inverse_transform containing core logic, called from inverse_transform

        Parameters
        ----------
        X: data structure of type X_inner_type
            if X_inner_type is list, _inverse_transform must support all types in it
            Data to be inverse transformed
        y : Series or Panel of type y_inner_type, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        inverse transformed version of X
        """
        return clone(self.transformer).fit(X=X, y=y).inverse_transform(X=X, y=y)

    def _get_fitted_params(self):
        """Get fitted parameters.

        Returns
        -------
        fitted_params : dict
        """
        return {}

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.
            There are currently no reserved values for transformers.

        Returns
        -------
        params : dict or list of dict, default = {}
            Parameters to create testing instances of the class
            Each dict are parameters to construct an "interesting" test instance, i.e.,
            `MyClass(**params)` or `MyClass(**params[i])` creates a valid test instance.
            `create_test_instance` uses the first (or only) dictionary in `params`
        """
        params = [
            {"transformer": _BoxCoxTransformer()},
            {"transformer": _BoxCoxTransformer(), "skip_inverse_transform": False},
        ]
        return params


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="MultiplexTransformer will be removed in version 0.11.0.",
    category=FutureWarning,
)
class MultiplexTransformer(_HeterogenousMetaEstimator, _DelegatedTransformer):
    """
    Facilitate an AutoML based selection of the best transformer.

    When used in combination with either TransformedTargetForecaster or
    ForecastingPipeline in combination with ForecastingGridSearchCV
    MultiplexTransformer provides a framework for transformer selection.  Through
    selection of the appropriate pipeline (ie TransformedTargetForecaster vs
    ForecastingPipeline) the transformers in MultiplexTransformer will either be
    applied to exogenous data, or to the target data.

    MultiplexTransformer delegates all transforming tasks (ie, calls to fit, transform,
    inverse_transform, and update) to a copy of the transformer in transformers
    whose name matches selected_transformer.  All other transformers in transformers
    will be ignored.

    Parameters
    ----------
    transformers : list of aeon transformers, or
        list of tuples (str, estimator) of named aeon transformers
        MultiplexTransformer can switch ("multiplex") between these transformers.
        Note - all the transformers passed in "transformers" should be thought of as
        blueprints.  Calling transformation functions on MultiplexTransformer will not
        change their state at all. - Rather a copy of each is created and this is what
        is updated.
    selected_transformer : str or None, optional, Default=None.
        If str, must be one of the transformer names.
            If passed in transformers were unnamed then selected_transformer must
            coincide with auto-generated name strings.
            To inspect auto-generated name strings, call get_params.
        If None, selected_transformer defaults to the name of the first transformer
           in transformers.
        selected_transformer represents the name of the transformer MultiplexTransformer
           should behave as (ie delegate all relevant transformation functionality to)

    Attributes
    ----------
    transformer_ : aeon transformer
        clone of the transformer named by selected_transformer to which all the
        transformation functionality is delegated to.
    _transformers : list of (name, est) tuples, where est are direct references to
        the estimators passed in transformers passed. If transformers was passed
        without names, those be auto-generated and put here.
    """

    # tags will largely be copied from selected_transformer
    _tags = {
        "fit_is_empty": False,
        "capability:multivariate": True,
        "X_inner_type": [
            "dask_panel",
            "pd-multiindex",
            "pd-long",
            "df-list",
            "xr.DataArray",
            "pd_multiindex_hier",
            "numpy3D",
            "np-list",
            "pd.DataFrame",
            "pd.Series",
            "dask_hierarchical",
            "np.ndarray",
            "dask_series",
            "nested_univ",
            "pd-wide",
        ],
    }

    # attribute for _DelegatedTransformer, which then delegates
    #     all non-overridden methods are same as of getattr(self, _delegate_name)
    #     see further details in _DelegatedTransformer docstring
    _delegate_name = "transformer_"

    # for default get_params/set_params from _HeterogenousMetaEstimator
    # _steps_attr points to the attribute of self
    # which contains the heterogeneous set of estimators
    # this must be an iterable of (name: str, estimator) pairs for the default
    _steps_attr = "_transformers"
    # if the estimator is fittable, _HeterogenousMetaEstimator also
    # provides an override for get_fitted_params for params from the fitted estimators
    # the fitted estimators should be in a different attribute, _steps_fitted_attr
    _steps_fitted_attr = "transformers_"

    def __init__(
        self,
        transformers: list,
        selected_transformer=None,
    ):
        super().__init__()
        self.selected_transformer = selected_transformer

        self.transformers = transformers
        self._check_estimators(
            transformers,
            attr_name="transformers",
            cls_type=BaseTransformer,
            clone_ests=False,
        )
        self._set_transformer()
        self.clone_tags(self.transformer_)
        self.set_tags(**{"fit_is_empty": False})
        # this ensures that we convert in the inner estimator, not in the multiplexer
        self.set_tags(**{"X_inner_type": ALL_TIME_SERIES_TYPES})

    @property
    def _transformers(self):
        """Forecasters turned into name/est tuples."""
        return self._get_estimator_tuples(self.transformers, clone_ests=False)

    @_transformers.setter
    def _transformers(self, value):
        self.transformers = value

    def _check_selected_transformer(self):
        component_names = self._get_estimator_names(
            self._transformers, make_unique=True
        )
        selected = self.selected_transformer
        if selected is not None and selected not in component_names:
            raise Exception(
                f"Invalid selected_transformer parameter value provided, "
                f" found: {selected}. Must be one of these"
                f" valid selected_transformer parameter values: {component_names}."
            )

    def _set_transformer(self):
        self._check_selected_transformer()
        # clone the selected transformer to self.transformer_
        if self.selected_transformer is not None:
            for name, transformer in self._get_estimator_tuples(self.transformers):
                if self.selected_transformer == name:
                    self.transformer_ = transformer.clone()
        else:
            # if None, simply clone the first transformer to self.transformer_
            self.transformer_ = self._get_estimator_list(self.transformers)[0].clone()

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.

        Returns
        -------
        params : dict or list of dict
        """
        from aeon.transformations.impute import Imputer

        # test with 2 simple detrend transformations with selected_transformer
        params1 = {
            "transformers": [
                ("imputer_mean", Imputer(method="mean")),
                ("imputer_near", Imputer(method="nearest")),
            ],
            "selected_transformer": "imputer_near",
        }
        # test no selected_transformer
        params2 = {
            "transformers": [
                Imputer(method="mean"),
                Imputer(method="nearest"),
            ],
        }
        return [params1, params2]

    def __or__(self, other):
        """Magic | (or) method, return (right) concatenated MultiplexTransformer.

        Implemented for `other` being a transformer, otherwise returns `NotImplemented`.

        Parameters
        ----------
        other: `aeon` transformer, must inherit from BaseTransformer
            otherwise, `NotImplemented` is returned

        Returns
        -------
        MultiplexTransformer object, concatenation of `self` (first) with `other`
            (last).not nested, contains only non-MultiplexTransformer `aeon`
            transformers

        Raises
        ------
        ValueError if other is not of type MultiplexTransformer or BaseTransformer.
        """
        other = _coerce_to_aeon(other)
        return self._dunder_concat(
            other=other,
            base_class=BaseTransformer,
            composite_class=MultiplexTransformer,
            attr_name="transformers",
            concat_order="left",
        )

    def __ror__(self, other):
        """Magic | (or) method, return (left) concatenated MultiplexTransformer.

        Implemented for `other` being a transformer, otherwise returns `NotImplemented`.

        Parameters
        ----------
        other: `aeon` transformer, must inherit from BaseTransformer
            otherwise, `NotImplemented` is returned

        Returns
        -------
        MultiplexTransformer object, concatenation of `self` (last) with `other`
            (first). not nested, contains only non-MultiplexTransformer `aeon`
            transformers
        """
        other = _coerce_to_aeon(other)
        return self._dunder_concat(
            other=other,
            base_class=BaseTransformer,
            composite_class=MultiplexTransformer,
            attr_name="forecasters",
            concat_order="right",
        )


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="InvertTransform will be removed in version 0.11.0.",
    category=FutureWarning,
)
class InvertTransform(_DelegatedTransformer):
    """Invert a series-to-series transformation.

    Switches `transform` and `inverse_transform`, leaves `fit` and `update` the same.

    Parameters
    ----------
    transformer : aeon transformer, must transform Series input to Series output
        this is a "blueprint" transformer, state does not change when `fit` is called

    Attributes
    ----------
    transformer_: transformer,
        this clone is fitted when `fit` is called and provides `transform` and inverse
    """

    _tags = {
        "input_data_type": "Series",
        # what is the abstract type of X: Series, or Panel
        "output_data_type": "Series",
        # what abstract type is returned: Primitives, Series, Panel
        "instancewise": True,  # is this an instance-wise transform?
        "X_inner_type": ["pd.DataFrame", "pd.Series"],
        "y_inner_type": "None",
        "capability:multivariate": True,
        "fit_is_empty": False,
        "capability:inverse_transform": True,
    }

    def __init__(self, transformer):
        self.transformer = transformer

        super().__init__()

        self.transformer_ = transformer.clone()

        # should be all tags, but not fit_is_empty
        #   (_fit should not be skipped)
        tags_to_clone = [
            "input_data_type",
            "output_data_type",
            "instancewise",
            "X_inner_type",
            "y_inner_type",
            "capability:missing_values",
            "X-y-must-have-same-index",
            "transform-returns-same-time-index",
            "skip-inverse-transform",
        ]
        self.clone_tags(transformer, tag_names=tags_to_clone)

        if not transformer.get_tag("capability:inverse_transform", False):
            warn(
                "transformer does not have capability to inverse transform, "
                "according to capability:inverse_transform tag. "
                "If the tag was correctly set, this transformer will likely crash"
            )
        inner_output = transformer.get_tag("output_data_type")
        if transformer.get_tag("output_data_type") != "Series":
            warn(
                f"transformer output is not Series but {inner_output}, "
                "according to output_data_type tag. "
                "The InvertTransform wrapper supports only Series output, therefore"
                " this transformer will likely crash on input."
            )

    # attribute for _DelegatedTransformer, which then delegates
    #     all non-overridden methods are same as of getattr(self, _delegate_name)
    #     see further details in _DelegatedTransformer docstring
    _delegate_name = "transformer_"

    def _transform(self, X, y=None):
        """Transform X and return a transformed version.

        private _transform containing the core logic, called from transform

        Returns a transformed version of X by iterating over specified
        columns and applying the wrapped transformer to them.

        Parameters
        ----------
        X : aeon compatible time series container
            Data to be transformed
        y : Series or Panel, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        Xt : aeon compatible time series container
            transformed version of X
        """
        return self.transformer_.inverse_transform(X=X, y=y)

    def _inverse_transform(self, X, y=None):
        """Logic used by `inverse_transform` to reverse transformation on `X`.

        Returns an inverse-transformed version of X by iterating over specified
        columns and applying the univariate series transformer to them.

        Only works if `self.transformer` has an `inverse_transform` method.

        Parameters
        ----------
        X : aeon compatible time series container
            Data to be inverse transformed
        y : Series or Panel, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        Xt : aeon compatible time series container
            inverse transformed version of X
        """
        return self.transformer_.transform(X=X, y=y)

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.

        Returns
        -------
        params : dict or list of dict, default = {}
            Parameters to create testing instances of the class
            Each dict are parameters to construct an "interesting" test instance, i.e.,
            `MyClass(**params)` or `MyClass(**params[i])` creates a valid test instance.
            `create_test_instance` uses the first (or only) dictionary in `params`
        """
        params1 = {"transformer": MockTransformer()}
        # _BoxCoxTransformer has fit
        params2 = {"transformer": _BoxCoxTransformer()}

        return [params1, params2]


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="Id will be removed in version 0.11.0.",
    category=FutureWarning,
)
class Id(BaseTransformer):
    """Identity transformer, returns data unchanged in transform/inverse_transform."""

    _tags = {
        "capability:inverse_transform": True,  # can the transformer inverse transform?
        "capability:multivariate": True,  # can the transformer handle multivariate X?
        "X_inner_type": ALL_TIME_SERIES_TYPES,
        "y_inner_type": "None",
        "fit_is_empty": True,  # is fit empty and can be skipped? Yes = True
        "transform-returns-same-time-index": True,
        # does transform return have the same time index as input X
        "capability:missing_values": True,  # can estimator handle missing data?
    }

    def _transform(self, X, y=None):
        """Transform X and return a transformed version.

        private _transform containing the core logic, called from transform

        Parameters
        ----------
        X : any aeon compatible data, Series, Panel, or Hierarchical
        y : optional, default=None
            ignored, argument present for interface conformance

        Returns
        -------
        X, identical to input
        """
        return X

    def _inverse_transform(self, X, y=None):
        """Inverse transform X and return an inverse transformed version.

        private _inverse_transform containing core logic, called from inverse_transform

        Parameters
        ----------
        X : any aeon compatible data, Series, Panel, or Hierarchical
        y : optional, default=None
            ignored, argument present for interface conformance

        Returns
        -------
        X, identical to input
        """
        return X

    def _get_fitted_params(self):
        """Get fitted parameters.

        private _get_fitted_params, called from get_fitted_params

        State required:
            Requires state to be "fitted".

        Returns
        -------
        fitted_params : dict
        """
        return {}


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="OptionalPassthrough will be removed in version 0.11.0.",
    category=FutureWarning,
)
class OptionalPassthrough(_DelegatedTransformer):
    """
    Wrap an existing transformer to tune whether to include it in a pipeline.

    Allows tuning the implicit hyperparameter whether or not to use a
    particular transformer inside a pipeline (e.g. TransformedTargetForecaster)
    or not. This is achieved by the hyperparameter `passthrough`
    which can be added to a tuning grid then (see example).

    Parameters
    ----------
    transformer : Estimator
        A scikit-learn-like or aeon-like transformer to fit and apply to series.
        this is a "blueprint" transformer, state does not change when `fit` is called.
    passthrough : bool, default=False
       Whether to apply the given transformer or to just
        passthrough the data (identity transformation). If, True the transformer
        is not applied and the OptionalPassthrough uses the identity
        transformation.

    Attributes
    ----------
    transformer_: transformer,
        this clone is fitted when `fit` is called and provides `transform` and inverse
        if passthrough = False, a clone of `transformer`passed
        if passthrough = True, the identity transformer `Id`
    """

    _tags = {
        "input_data_type": "Series",
        # what is the abstract type of X: Series, or Panel
        "output_data_type": "Series",
        # what abstract type is returned: Primitives, Series, Panel
        "instancewise": True,
        "X_inner_type": ALL_TIME_SERIES_TYPES,
        "y_inner_type": "None",
        "capability:multivariate": True,
        "fit_is_empty": False,
        "capability:inverse_transform": True,
    }

    def __init__(self, transformer, passthrough=False):
        self.transformer = transformer
        self.passthrough = passthrough

        super().__init__()

        # should be all tags, but not fit_is_empty
        #   (_fit should not be skipped)
        tags_to_clone = [
            "input_data_type",
            "output_data_type",
            "instancewise",
            "y_inner_type",
            "capability:inverse_transform",
            "capability:missing_values",
            "X-y-must-have-same-index",
            "transform-returns-same-time-index",
            "skip-inverse-transform",
        ]
        self.clone_tags(transformer, tag_names=tags_to_clone)

        if passthrough:
            self.transformer_ = Id()
        else:
            self.transformer_ = transformer.clone()

    # attribute for _DelegatedTransformer, which then delegates
    #     all non-overridden methods are same as of getattr(self, _delegate_name)
    #     see further details in _DelegatedTransformer docstring
    _delegate_name = "transformer_"

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.


        Returns
        -------
        params : dict or list of dict, default = {}
            Parameters to create testing instances of the class
            Each dict are parameters to construct an "interesting" test instance, i.e.,
            `MyClass(**params)` or `MyClass(**params[i])` creates a valid test instance.
            `create_test_instance` uses the first (or only) dictionary in `params`
        """
        return {"transformer": _BoxCoxTransformer(), "passthrough": False}


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="ColumnwiseTransformer will be removed in version 0.11.0.",
    category=FutureWarning,
)
class ColumnwiseTransformer(BaseTransformer):
    """Apply a transformer columnwise to multivariate series.

    Overview: input multivariate time series and the transformer passed
    in `transformer` parameter is applied to specified `columns`, each
    column is handled as a univariate series. The resulting transformed
    data has the same shape as input data.

    Parameters
    ----------
    transformer : Estimator
        scikit-learn-like or aeon-like transformer to fit and apply to series.
    columns : list of str or None
            Names of columns that are supposed to be transformed.
            If None, all columns are transformed.

    Attributes
    ----------
    transformers_ : dict of {str : transformer}
        Maps columns to transformers.
    columns_ : list of str
        Names of columns that are supposed to be transformed.

    See Also
    --------
    OptionalPassthrough
    """

    _tags = {
        "input_data_type": "Series",
        # what is the abstract type of X: Series, or Panel
        "output_data_type": "Series",
        # what abstract type is returned: Primitives, Series, Panel
        "instancewise": True,  # is this an instance-wise transform?
        "X_inner_type": "pd.DataFrame",
        "y_inner_type": "None",
        "capability:multivariate": True,
        "fit_is_empty": False,
    }

    def __init__(self, transformer, columns=None):
        self.transformer = transformer
        self.columns = columns
        super().__init__()

        tags_to_clone = [
            "y_inner_type",
            "capability:inverse_transform",
            "capability:missing_values",
            "X-y-must-have-same-index",
            "transform-returns-same-time-index",
            "skip-inverse-transform",
        ]
        self.clone_tags(transformer, tag_names=tags_to_clone)

    def _fit(self, X, y=None):
        """Fit transformer to X and y.

        private _fit containing the core logic, called from fit

        Parameters
        ----------
        X : pd.DataFrame
            Data to fit transform to
        y : Series or Panel, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        self: a fitted instance of the estimator
        """
        # check that columns are None or list of strings
        if self.columns is not None:
            if not isinstance(self.columns, list) and all(
                isinstance(s, str) for s in self.columns
            ):
                raise ValueError("Columns need to be a list of strings or None.")

        # set self.columns_ to columns that are going to be transformed
        # (all if self.columns is None)
        self.columns_ = self.columns
        if self.columns_ is None:
            self.columns_ = X.columns

        # make sure z contains all columns that the user wants to transform
        _check_columns(X, selected_columns=self.columns_)

        # fit by iterating over columns
        self.transformers_ = {}
        for colname in self.columns_:
            transformer = self.transformer.clone()
            self.transformers_[colname] = transformer
            self.transformers_[colname].fit(X[colname], y)
        return self

    def _transform(self, X, y=None):
        """Transform X and return a transformed version.

        private _transform containing the core logic, called from transform

        Returns a transformed version of X by iterating over specified
        columns and applying the wrapped transformer to them.

        Parameters
        ----------
        X : pd.DataFrame
            Data to be transformed
        y : Series or Panel, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        Xt : pd.DataFrame
            transformed version of X
        """
        # make copy of z
        X = X.copy()

        # make sure z contains all columns that the user wants to transform
        _check_columns(X, selected_columns=self.columns_)
        for colname in self.columns_:
            X[colname] = self.transformers_[colname].transform(X[colname], y)
        return X

    def _inverse_transform(self, X, y=None):
        """Logic used by `inverse_transform` to reverse transformation on `X`.

        Returns an inverse-transformed version of X by iterating over specified
        columns and applying the univariate series transformer to them.
        Only works if `self.transformer` has an `inverse_transform` method.

        Parameters
        ----------
        X : pd.DataFrame
            Data to be inverse transformed
        y : Series or Panel, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        Xt : pd.DataFrame
            inverse transformed version of X
        """
        # make copy of z
        X = X.copy()

        # make sure z contains all columns that the user wants to transform
        _check_columns(X, selected_columns=self.columns_)

        # iterate over columns that are supposed to be inverse_transformed
        for colname in self.columns_:
            X[colname] = self.transformers_[colname].inverse_transform(X[colname], y)

        return X

    def update(self, X, y=None, update_params=True):
        """Update parameters.

        Update the parameters of the estimator with new data
        by iterating over specified columns.
        Only works if `self.transformer` has an `update` method.

        Parameters
        ----------
        X : pd.Series
            New time series.
        update_params : bool, optional, default=True

        Returns
        -------
        self : an instance of self
        """
        z = check_series(X)

        # make z a pd.DataFrame in univariate case
        if isinstance(z, pd.Series):
            z = z.to_frame()

        # make sure z contains all columns that the user wants to transform
        _check_columns(z, selected_columns=self.columns_)
        for colname in self.columns_:
            self.transformers_[colname].update(z[colname], X)
        return self

    @classmethod
    def get_test_params(cls, parameter_set="default"):
        """Return testing parameter settings for the estimator.

        Parameters
        ----------
        parameter_set : str, default="default"
            Name of the set of test parameters to return, for use in tests. If no
            special parameters are defined for a value, will return `"default"` set.


        Returns
        -------
        params : dict or list of dict, default = {}
            Parameters to create testing instances of the class
            Each dict are parameters to construct an "interesting" test instance, i.e.,
            `MyClass(**params)` or `MyClass(**params[i])` creates a valid test instance.
            `create_test_instance` uses the first (or only) dictionary in `params`
        """
        from aeon.transformations.detrend import Detrender

        return {"transformer": Detrender()}


def _check_columns(z, selected_columns):
    # make sure z contains all columns that the user wants to transform
    z_wanted_keys = set(selected_columns)
    z_new_keys = set(z.columns)
    difference = z_wanted_keys.difference(z_new_keys)
    if len(difference) != 0:
        raise ValueError("Missing columns" + str(difference) + "in Z.")


def _check_is_pdseries(z):
    # make z a pd.Dataframe in univariate case
    is_series = False
    if isinstance(z, pd.Series):
        z = z.to_frame()
        is_series = True
    return z, is_series


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="ColumnConcatenator will be removed in version 0.11.0.",
    category=FutureWarning,
)
class ColumnConcatenator(BaseTransformer):
    """Concatenate multivariate series to a long univariate series.

    Transformer that concatenates multivariate time series/panel data
    into long univariate time series/panel
        data by simply concatenating times series in time.
    """

    _tags = {
        "input_data_type": "Series",
        # what is the abstract type of X: Series, or Panel
        "output_data_type": "Series",
        # what abstract type is returned: Primitives, Series, Panel
        "instancewise": False,
        "X_inner_type": ["pd-multiindex", "pd_multiindex_hier"],
        "y_inner_type": "None",
        "fit_is_empty": True,
    }

    def _transform(self, X, y=None):
        """Transform the data.

        Concatenate multivariate time series/panel data into long
        univariate time series/panel
        data by simply concatenating times series in time.

        Parameters
        ----------
        X : nested pandas DataFrame of shape [n_samples, n_features]
            Nested dataframe with time-series in cells.

        Returns
        -------
        Xt : pandas DataFrame
          Transformed pandas DataFrame with same number of rows and single
          column
        """
        Xst = pd.DataFrame(X.stack())
        Xt = Xst.swaplevel(-2, -1).sort_index().droplevel(-2)

        # the above has the right structure, but the wrong indes
        # the time index is in general non-unique now, we replace it by integer index
        inst_idx = Xt.index.get_level_values(0)
        t_idx = [range(len(Xt.loc[x])) for x in inst_idx.unique()]
        t_idx = np.concatenate(t_idx)

        Xt.index = pd.MultiIndex.from_arrays([inst_idx, t_idx])
        Xt.index.names = X.index.names
        return Xt


# TODO: remove in v0.11.0
@deprecated(
    version="0.10.0",
    reason="OptionalPassthrough will be removed in version 0.11.0.",
    category=FutureWarning,
)
class YtoX(BaseTransformer):
    """
    Create exogeneous features which are a copy of the endogenous data.

    Replaces exogeneous features (`X`) by endogeneous data (`y`).

    To *add* instead of *replace*, use `FeatureUnion`.

    Parameters
    ----------
    subset_index : bool, default=False
        if True, subsets the output of `transform` to `X.index`,
        i.e., outputs `y.loc[X.index]`.
    """

    _tags = {
        "transform-returns-same-time-index": True,
        "skip-inverse-transform": False,
        "capability:multivariate": True,
        "X_inner_type": ["pd.DataFrame", "pd-multiindex", "pd_multiindex_hier"],
        "y_inner_type": ["pd.DataFrame", "pd-multiindex", "pd_multiindex_hier"],
        "y_input_type": "both",
        "fit_is_empty": True,
        "requires_y": True,
    }

    def __init__(self, subset_index=False):
        self.subset_index = subset_index

        super().__init__()

    def _transform(self, X, y=None):
        """Transform X and return a transformed version.

        private _transform containing core logic, called from transform

        Parameters
        ----------
        X : time series or panel in one of the pd.DataFrame formats
            Data to be transformed
        y : time series or panel in one of the pd.DataFrame formats
            Additional data, e.g., labels for transformation

        Returns
        -------
        y, as a transformed version of X
        """
        if self.subset_index:
            return y.loc[X.index.intersection(y.index)]
        else:
            return y

    def _inverse_transform(self, X, y=None):
        """Inverse transform, inverse operation to transform.

        Drops featurized column that was added in transform().

        Parameters
        ----------
        X: data structure of type X_inner_type
            if X_inner_type is list, _inverse_transform must support all types in it
            Data to be inverse transformed
        y : Series or Panel of type y_inner_type, default=None
            Additional data, e.g., labels for transformation

        Returns
        -------
        inverse transformed version of X
        """
        if self.subset_index:
            return y.loc[X.index.intersection(y.index)]
        else:
            return y
