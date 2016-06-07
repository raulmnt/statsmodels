"""
Markov switching autoregression models

Author: Chad Fulton
License: BSD-3
"""

from __future__ import division, absolute_import, print_function

import numpy as np
import statsmodels.base.wrapper as wrap

from statsmodels.tsa.tsatools import lagmat
from statsmodels.tsa.regime_switching import (
    markov_switching, markov_regression)
from statsmodels.tsa.statespace.tools import (
    constrain_stationary_univariate, unconstrain_stationary_univariate)


class MarkovAutoregression(markov_regression.MarkovRegression):
    """
    Markov switching regression model

    Parameters
    ----------
    endog : array_like
        The endogenous variable.
    k_regimes : integer
        The number of regimes.
    order : integer
        The order of the autoregressive lag polynomial.
    trend : {'nc', 'c', 't', 'ct'}
        Whether or not to include a trend. To include an constant, time trend,
        or both, set `trend='c'`, `trend='t'`, or `trend='ct'`. For no trend,
        set `trend='nc'`. Default is a constant.
    exog : array_like, optional
        Array of exogenous regressors, shaped nobs x k.
    exog_tvtp : array_like, optional
        Array of exogenous or lagged variables to use in calculating
        time-varying transition probabilities (TVTP). TVTP is only used if this
        variable is provided. If an intercept is desired, a column of ones must
        be explicitly included in this array.
    switching_ar : boolean or iterable, optional
        If a boolean, sets whether or not all autoregressive coefficients are
        switching across regimes. If an iterable, should be of length equal
        to `order`, where each element is a boolean describing whether the
        corresponding coefficient is switching. Default is True.
    switching_trend : boolean or iterable, optional
        If a boolean, sets whether or not all trend coefficients are
        switching across regimes. If an iterable, should be of length equal
        to the number of trend variables, where each element is
        a boolean describing whether the corresponding coefficient is
        switching. Default is True.
    switching_exog : boolean or iterable, optional
        If a boolean, sets whether or not all regression coefficients are
        switching across regimes. If an iterable, should be of length equal
        to the number of exogenous variables, where each element is
        a boolean describing whether the corresponding coefficient is
        switching. Default is True.
    switching_variance : boolean, optional
        Whether or not there is regime-specific heteroskedasticity, i.e.
        whether or not the error term has a switching variance. Default is
        False.

    Notes
    -----
    The `trend` is accomodated by prepending columns to the `exog` array. Thus
    if `trend='c'`, the passed `exog` array should not already have a column of
    ones.

    References
    ----------
    Kim, Chang-Jin, and Charles R. Nelson. 1999.
    "State-Space Models with Regime Switching:
    Classical and Gibbs-Sampling Approaches with Applications".
    MIT Press Books. The MIT Press.

    """

    def __init__(self, endog, k_regimes, order, trend='c', exog=None,
                 exog_tvtp=None, switching_ar=True, switching_trend=True,
                 switching_exog=False, switching_variance=False,
                 dates=None, freq=None, missing='none'):

        # Properties
        self.switching_ar = switching_ar

        # Switching options
        if self.switching_ar is True or self.switching_ar is False:
            self.switching_ar = [self.switching_ar] * order
        elif not len(self.switching_ar) == order:
            raise ValueError('Invalid iterable passed to `switching_ar`.')

        # Initialize the base model
        super(MarkovAutoregression, self).__init__(
            endog, k_regimes, trend=trend, exog=exog, order=order,
            exog_tvtp=exog_tvtp, switching_trend=switching_trend,
            switching_exog=switching_exog,
            switching_variance=switching_variance, dates=dates, freq=freq,
            missing=missing)

        # Sanity checks
        if self.nobs <= self.order:
            raise ValueError('Must have more observations than the order of'
                             ' the autoregression.')

        # Autoregressive exog
        self.exog_ar = lagmat(endog, self.order)[self.order:]

        # Reshape other datasets
        self.nobs -= self.order
        self.orig_endog = self.endog
        self.endog = self.endog[self.order:]
        if self._k_exog > 0:
            self.orig_exog = self.exog
            self.exog = self.exog[self.order:]

        # Reset the ModelData datasets
        self.data.endog, self.data.exog = (
            self.data._convert_endog_exog(self.endog, self.exog))

        # Reset dates, if provided
        if self.data.dates is not None:
            new_length = self.data.endog.shape[0]
            self.data.dates = self.data.dates[self.order:]

        # Parameters
        self.parameters['autoregressive'] = self.switching_ar

        # Cache an array for holding residuals (no need to create it each time)
        # We need to compute a different set of residuals for each of the
        # possibly histories S_t, S_{t-1}, ..., S_{t-p}
        # self._resid = np.zeros(
        #     (self.k_regimes,) * (self.order + 1) + (self.nobs,))
        self._resid_slices = [slice(None, None, None)] * (self.order + 1)

    def _resid(self, params):
        """
        Compute residuals conditional on the current period's regime and
        the last `self.order` regimes.
        """
        params = np.array(params, ndmin=1)

        # Prediction is based on:
        # y_t = x_t beta^{(S_t)} +
        #       \phi_1^{(S_t)} (y_{t-1} - x_{t-1} beta^{(S_t-1)}) + ...
        #       \phi_p^{(S_t)} (y_{t-p} - x_{t-p} beta^{(S_t-p)}) + eps_t
        # self._resid is a (k_regimes x ... x k_regimes x nobs) matrix
        # with dimension self.order + 2
        # self._resid[0, ...] corresponds to S_{t} = 0, etc.
        if self._k_exog > 0:
            xb = []
            for i in range(self.k_regimes):
                coeffs = params[self.parameters[i, 'exog']]
                xb.append(np.dot(self.orig_exog, coeffs))

        resid = np.zeros(
            (self.k_regimes,) * (self.order + 1) + (self.nobs,),
            dtype=np.promote_types(np.float64, params.dtype))
        resid[:] = self.endog
        # Iterate over S_{t} = i
        for i in range(self.k_regimes):
            ar_coeffs = params[self.parameters[i, 'autoregressive']]

            # y_t - x_t beta^{(S_t)}
            ix = self._resid_slices[:]
            ix[0] = i
            ix = tuple(ix)
            if self._k_exog > 0:
                resid[ix] -= xb[i][self.order:]

            # Iterate over j = 2, .., p
            for j in range(1, self.order + 1):
                for k in range(self.k_regimes):
                    # This gets a specific time-period / regime slice:
                    # S_{t} = i, S_{t-j} = k, across all other time-period /
                    # regime slices.
                    ix = self._resid_slices[:]
                    ix[0] = i
                    ix[j] = k
                    ix = tuple(ix)

                    start = self.order - j
                    end = -j
                    if self._k_exog > 0:
                        resid[ix] -= ar_coeffs[j-1] * (
                            self.orig_endog[start:end] - xb[k][start:end])
                    else:
                        resid[ix] -= ar_coeffs[j-1] * (
                            self.orig_endog[start:end])

        return resid

    def _conditional_likelihoods(self, params):
        """
        Compute likelihoods conditional on the current period's regime and
        the last `self.order` regimes.
        """
        # Get the residuals
        resid = self._resid(params)

        # Compute the conditional likelihoods
        variance = params[self.parameters['variance']].squeeze()
        if self.switching_variance:
            variance = np.reshape(variance, (self.k_regimes, 1, 1))

        conditional_likelihoods = (
            np.exp(-0.5 * resid**2 / variance) / np.sqrt(2 * np.pi * variance))

        return conditional_likelihoods

    def filter(self, *args, **kwargs):
        kwargs.setdefault('results_class', MarkovAutoregressionResults)
        kwargs.setdefault('results_wrapper_class',
                          MarkovAutoregressionResultsWrapper)
        return super(MarkovAutoregression, self).filter(*args, **kwargs)

    def smooth(self, *args, **kwargs):
        kwargs.setdefault('results_class', MarkovAutoregressionResults)
        kwargs.setdefault('results_wrapper_class',
                          MarkovAutoregressionResultsWrapper)
        return super(MarkovAutoregression, self).smooth(*args, **kwargs)

    def _em_iteration(self, params0):
        """
        EM iteration
        """
        # Inherited parameters
        result, params1 = markov_switching.MarkovSwitching._em_iteration(
            self, params0)

        tmp = np.sqrt(result.smoothed_marginal_probabilities)

        # Regression coefficients
        coeffs = None
        if self._k_exog > 0:
            coeffs = self._em_exog(result, self.endog, self.exog,
                                   self.parameters.switching['exog'], tmp)
            for i in range(self.k_regimes):
                params1[self.parameters[i, 'exog']] = coeffs[i]

        # Autoregressive
        if self.order > 0:
            if True or self._k_exog > 0:
                ar_coeffs, variance = self._em_autoregressive(
                    result, coeffs)
            else:
                ar_coeffs = self._em_exog(
                    result, self.endog, self.exog_ar,
                    self.parameters.switching['autoregressive'])
                variance = self._em_variance(
                    result, self.endog, self.exog_ar, ar_coeffs, tmp)
            for i in range(self.k_regimes):
                params1[self.parameters[i, 'autoregressive']] = ar_coeffs[i]
            params1[self.parameters['variance']] = variance

        return result, params1

    def _em_autoregressive(self, result, betas, tmp=None):
        """
        EM step for autoregressive coefficients and variances
        """
        if tmp is None:
            tmp = np.sqrt(result.smoothed_marginal_probabilities)

        resid = np.zeros((self.k_regimes, self.nobs + self.order))
        resid[:] = self.orig_endog
        if self._k_exog > 0:
            for i in range(self.k_regimes):
                resid[i] -= np.dot(self.orig_exog, betas[i])

        # The difference between this and `_em_exog` is that here we have a
        # different endog and exog for each regime
        coeffs = np.zeros((self.k_regimes,) + (self.order,))
        variance = np.zeros((self.k_regimes,))
        exog = np.zeros((self.nobs, self.order))
        for i in range(self.k_regimes):
            endog = resid[i, self.order:]
            exog = lagmat(resid[i], self.order)[self.order:]
            tmp_endog = tmp[i] * endog
            tmp_exog = tmp[i][:, None] * exog

            coeffs[i] = np.dot(np.linalg.pinv(tmp_exog), tmp_endog)

            if self.switching_variance:
                tmp_resid = endog - np.dot(exog, coeffs[i])
                variance[i] = (np.sum(
                    tmp_resid**2 * result.smoothed_marginal_probabilities[i]) /
                    np.sum(result.smoothed_marginal_probabilities[i]))
            else:
                tmp_resid = tmp_endog - np.dot(tmp_exog, coeffs[i])
                variance[i] = np.sum(tmp_resid**2)

        # Variances
        if not self.switching_variance:
            variance = variance.sum() / self.nobs

        return coeffs, variance

    @property
    def start_params(self):
        """
        (array) Starting parameters for maximum likelihood estimation.
        """
        # Inherited parameters
        params = markov_switching.MarkovSwitching.start_params.fget(self)

        # OLS for starting parameters
        endog = self.endog.copy()
        if self._k_exog > 0 and self.order > 0:
            exog = np.c_[self.exog, self.exog_ar]
        elif self._k_exog > 0:
            exog = self.exog
        elif self.order > 0:
            exog = self.exog_ar

        if self._k_exog > 0 or self.order > 0:
            beta = np.dot(np.linalg.pinv(exog), endog)
            variance = np.var(endog - np.dot(exog, beta))
        else:
            variance = np.var(endog)

        # Regression coefficients
        if self._k_exog > 0:
            if np.any(self.switching_coeffs):
                for i in range(self.k_regimes):
                    params[self.parameters[i, 'exog']] = (
                        beta[:self._k_exog] * (i / self.k_regimes))
            else:
                params[self.parameters['exog']] = beta[:self._k_exog]

        # Autoregressive
        if self.order > 0:
            if np.any(self.switching_ar):
                for i in range(self.k_regimes):
                    params[self.parameters[i, 'autoregressive']] = (
                        beta[self._k_exog:] * (i / self.k_regimes))
            else:
                params[self.parameters['autoregressive']] = beta[self._k_exog:]

        # Variance
        if self.switching_variance:
            params[self.parameters['variance']] = (
                np.linspace(variance / 10., variance, num=self.k_regimes))
        else:
            params[self.parameters['variance']] = variance

        return params

    @property
    def param_names(self):
        """
        (list of str) List of human readable parameter names (for parameters
        actually included in the model).
        """
        # Inherited parameters
        param_names = np.array(
            markov_regression.MarkovRegression.param_names.fget(self),
            dtype=object)

        # Autoregressive
        if np.any(self.switching_ar):
            for i in range(self.k_regimes):
                param_names[self.parameters[i, 'autoregressive']] = [
                    'ar.L%d[%d]' % (j+1, i) for j in range(self.order)]
        else:
            param_names[self.parameters['autoregressive']] = [
                'ar.L%d' % (j+1) for j in range(self.order)]

        return param_names.tolist()

    def transform_params(self, unconstrained):
        """
        Transform unconstrained parameters used by the optimizer to constrained
        parameters used in likelihood evaluation

        Parameters
        ----------
        unconstrained : array_like
            Array of unconstrained parameters used by the optimizer, to be
            transformed.

        Returns
        -------
        constrained : array_like
            Array of constrained parameters which may be used in likelihood
            evalation.
        """
        # Inherited parameters
        constrained = super(MarkovAutoregression, self).transform_params(
            unconstrained)

        # Autoregressive
        # TODO may provide unexpected results when some coefficients are not
        # switching
        for i in range(self.k_regimes):
            s = self.parameters[i, 'autoregressive']
            constrained[s] = constrain_stationary_univariate(
                unconstrained[s])

        return constrained

    def untransform_params(self, constrained):
        """
        Transform constrained parameters used in likelihood evaluation
        to unconstrained parameters used by the optimizer

        Parameters
        ----------
        constrained : array_like
            Array of constrained parameters used in likelihood evalution, to be
            transformed.

        Returns
        -------
        unconstrained : array_like
            Array of unconstrained parameters used by the optimizer.
        """
        # Inherited parameters
        unconstrained = super(MarkovAutoregression, self).untransform_params(
            constrained)

        # Autoregressive
        # TODO may provide unexpected results when some coefficients are not
        # switching
        for i in range(self.k_regimes):
            s = self.parameters[i, 'autoregressive']
            unconstrained[s] = unconstrain_stationary_univariate(
                constrained[s])

        return unconstrained


class MarkovAutoregressionResults(markov_regression.MarkovRegressionResults):
    r"""
    Class to hold results from fitting a Markov switching autoregression model

    Parameters
    ----------
    model : MarkovAutoregression instance
        The fitted model instance
    params : array
        Fitted parameters
    filter_results : HamiltonFilterResults or KimSmootherResults instance
        The underlying filter and, optionally, smoother output
    cov_type : string
        The type of covariance matrix estimator to use. Can be one of 'approx',
        'opg', 'robust', or 'none'.

    Attributes
    ----------
    model : Model instance
        A reference to the model that was fit.
    filter_results : HamiltonFilterResults or KimSmootherResults instance
        The underlying filter and, optionally, smoother output
    nobs : float
        The number of observations used to fit the model.
    params : array
        The parameters of the model.
    scale : float
        This is currently set to 1.0 and not used by the model or its results.

    """
    pass


class MarkovAutoregressionResultsWrapper(
        markov_regression.MarkovRegressionResultsWrapper):
    pass
wrap.populate_wrapper(MarkovAutoregressionResultsWrapper,
                      MarkovAutoregressionResults)
