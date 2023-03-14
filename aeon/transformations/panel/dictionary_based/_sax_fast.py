# -*- coding: utf-8 -*-
import math

import numpy as np
import pandas as pd
import scipy.stats
from numba import njit
from scipy.sparse import csr_matrix

from aeon.transformations.base import BaseTransformer

__author__ = ["patrickzib", "MatthewMiddlehurst"]


class SAX_FAST(BaseTransformer):
    """Symbolic Aggregate approXimation (SAX) transformer.

    as described in
    Jessica Lin, Eamonn Keogh, Li Wei and Stefano Lonardi,
    "Experiencing SAX: a novel symbolic representation of time series"
    Data Mining and Knowledge Discovery, 15(2):107-144
    Overview: for each series:
        run a sliding window across the series
        for each window
            shorten the series with PAA (Piecewise Approximate Aggregation)
            discretise the shortened series into fixed bins
            form a word from these discrete values
    by default SAX produces a single word per series (window_size=0).
    SAX returns a pandas data frame where column 0 is the histogram (sparse
    pd.series)
    of each series.

    Parameters
    ----------
        word_length:         int
            length of word to shorten window to (using PAA) (default 8)

        alphabet_size:       int
            number of values to discretise each value to (default to 4)

        window_size:         int
            size of window for sliding. Input series length for whole series
            transform (default to 12)

        remove_repeat_words: boolean
            whether to use numerosity reduction (default False)

        save_words:     boolean,
            whether to use numerosity reduction (default False)

        return_sparse:  boolean, default=True
            if set to true, a scipy sparse matrix will be returned as BOP model.
            If set to false a dense array will be returned as BOP model. Sparse
            arrays are much more compact.

        return_pandas_data_series:          boolean, default = True
            set to true to return Pandas Series as a result of transform.
            setting to true reduces speed significantly but is required for
            automatic test.

    Attributes
    ----------
        words:      history = []

    """

    _tags = {
        "univariate-only": True,
        "fit_is_empty": True,
        "scitype:transform-input": "Series",
        # what is the scitype of X: Series, or Panel
        "scitype:transform-output": "Series",
        # what scitype is returned: Primitives, Series, Panel
        "scitype:instancewise": True,  # is this an instance-wise transform?
        "X_inner_mtype": "numpy3D",  # which mtypes do _fit/_predict support for X?
        "y_inner_mtype": "None",  # which mtypes do _fit/_predict require for y?
    }

    def __init__(
        self,
        word_length=8,
        alphabet_size=4,
        window_size=12,
        remove_repeat_words=False,
        return_sparse=True,
        save_words=False,
        return_pandas_data_series=False,
    ):
        self.word_length = word_length
        self.alphabet_size = alphabet_size
        self.window_size = window_size
        self.remove_repeat_words = remove_repeat_words
        self.return_sparse = return_sparse
        self.save_words = save_words
        self.return_pandas_data_series = return_pandas_data_series
        self.words = []
        self.letter_bits = 0

        super(SAX_FAST, self).__init__(_output_convert="off")

    def _transform(self, X, y=None):
        """Transform data.

        Parameters
        ----------
        X : nested pandas DataFrame of shape [n_instances, 1]
            Nested dataframe with univariate time-series in cells.

        Returns
        -------
        dims: Pandas data frame with first dimension in column zero
        """
        X = X.squeeze(1)

        if self.alphabet_size < 2 or self.alphabet_size > 4:
            raise RuntimeError("Alphabet size must be an integer between 2 and 4")
        if self.word_length < 1 or self.word_length > 16:
            raise RuntimeError("Word length must be an integer between 1 and 16")

        breakpoints = self._generate_breakpoints()
        n_instances, series_length = X.shape

        num_windows_per_inst = series_length - self.window_size + 1
        all_words = np.zeros((n_instances, num_windows_per_inst), dtype=np.int_)
        self.letter_bits = np.uint32(math.ceil(math.log2(self.alphabet_size)))

        for i in range(n_instances):
            split = np.array(
                X[
                    i,
                    np.arange(self.window_size)[None, :]
                    + np.arange(num_windows_per_inst)[:, None],
                ]
            ).astype(np.float_)

            split = scipy.stats.zscore(split, axis=1)
            patterns = SAX_FAST._perform_paa_along_dim(split, self.word_length)

            for n in range(patterns.shape[0]):
                all_words[i, n] = SAX_FAST._generate_words(
                    patterns[n, :], self.word_length, breakpoints, self.letter_bits
                )

        if self.remove_repeat_words:
            all_words = SAX_FAST._remove_repeating_words(all_words)

        if self.save_words:
            self.words = all_words

        if self.return_pandas_data_series:
            bb = pd.DataFrame()
            bb[0] = [pd.Series(bag) for bag in all_words]
            return bb
        elif self.return_sparse:
            all_words = csr_matrix(all_words, dtype=np.uint32)

        return all_words

    @staticmethod
    @njit(fastmath=True, cache=True)  # parallel=True,
    def _perform_paa_along_dim(X, num_intervals):
        num_insts = X.shape[0]
        num_atts = X.shape[1]
        data = np.zeros((num_insts, num_intervals))
        for i in range(num_insts):
            series = X[i, :]
            current_frame = 0
            current_frame_size = 0
            frame_length = num_atts / num_intervals
            frame_sum = 0

            for n in range(num_atts):
                remaining = frame_length - current_frame_size

                if remaining > 1:
                    frame_sum += series[n]
                    current_frame_size += 1
                else:
                    frame_sum += remaining * series[n]
                    current_frame_size += remaining

                if current_frame_size == frame_length:
                    data[i, current_frame] = frame_sum / frame_length
                    current_frame += 1
                    frame_sum = (1 - remaining) * series[n]
                    current_frame_size = 1 - remaining

            # if the last frame was lost due to double imprecision
            if current_frame == num_intervals - 1:
                data[i, current_frame] = frame_sum / frame_length

        return data

    @staticmethod
    @njit(fastmath=True, cache=True)
    def _remove_repeating_words(words):
        for i in range(words.shape[0]):
            last_word = 0
            for j in range(words.shape[1]):
                if last_word == words[i, j]:
                    # We encode the repeated words as 0 and remove them
                    # This is implementged using np.nonzero in numba. Thus must be 0
                    words[i, j] = 0
                last_word = words[i, j]

        return words

    @staticmethod
    @njit(fastmath=True, cache=True)  # parallel=True,
    def _generate_words(pattern, word_length, breakpoints, letter_bits):
        word = np.int32(0)
        for i in range(word_length):
            for bp in range(len(breakpoints)):
                if pattern[i] <= breakpoints[bp]:
                    word = (word << letter_bits) | bp  # TODO
                    break

        return word

    def _generate_breakpoints(self):
        # Pre-made gaussian curve breakpoints from UEA TSC codebase
        return np.array(
            {
                2: [0, np.inf],
                3: [-0.43, 0.43, np.inf],
                4: [-0.67, 0, 0.67, np.inf],
                5: [-0.84, -0.25, 0.25, 0.84, np.inf],
                6: [-0.97, -0.43, 0, 0.43, 0.97, np.inf],
                7: [-1.07, -0.57, -0.18, 0.18, 0.57, 1.07, np.inf],
                8: [-1.15, -0.67, -0.32, 0, 0.32, 0.67, 1.15, np.inf],
                9: [-1.22, -0.76, -0.43, -0.14, 0.14, 0.43, 0.76, 1.22, np.inf],
                10: [-1.28, -0.84, -0.52, -0.25, 0.0, 0.25, 0.52, 0.84, 1.28, np.inf],
            }[self.alphabet_size]
        )

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
        # small word length, window size for testing
        params = {"word_length": 2, "window_size": 4}
        return params
