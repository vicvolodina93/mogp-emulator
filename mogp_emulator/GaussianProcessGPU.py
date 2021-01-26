"""
extends GaussianProcess with an (optional) GPU implementation
"""

import os
import numpy as np
from mogp_emulator.MeanFunction import MeanFunction, MeanBase
from mogp_emulator.Kernel import Kernel, SquaredExponential, Matern52
from mogp_emulator.Priors import Prior
from scipy import linalg
from scipy.optimize import OptimizeResult

import libgpgpu

from mogp_emulator.GaussianProcess import PredictResult


class GPUUnavailableError(RuntimeError):
    """Exception type to use when a GPU, or the GPU library, is unavailable"""
    pass


class GaussianProcessGPU(object):
    """
    This class implements the same interface as
    :class:`mogp_emulator.GaussianProcess.GaussianProcess`, but with
    particular methods overridden to use a GPU if it is available.
    """

    def __init__(self, inputs, targets, mean=None, kernel=SquaredExponential(), priors=None,
                 nugget="adaptive", inputdict = {}, use_patsy=True):
        inputs = np.array(inputs)
        if inputs.ndim == 1:
            inputs = np.reshape(inputs, (-1, 1))
        assert inputs.ndim == 2

        targets = np.array(targets)
        assert targets.ndim == 1
        assert targets.shape[0] == inputs.shape[0]

        if mean:
            raise ValueError("GPU implementation requires mean to be None")

        if isinstance(kernel, str):
            if kernel == "SquaredExponential":
                kernel = SquaredExponential()
            else:
                raise ValueError("GPU implementation requires kernel to be SquaredExponential")
        elif kernel and not isinstance(kernel, SquaredExponential):
                raise ValueError("GPU implementation requires kernel to be SquaredExponential()")
        self.kernel=kernel
        self.nugget = nugget
        # instantiate the C++ class
        self._densegp_gpu = libgpgpu.DenseGP_GPU(inputs, targets)

    @property
    def inputs(self):
        """
        Returns inputs for the emulator as a numpy array

        :returns: Emulator inputs, 2D array with shape ``(n, D)``
        :rtype: ndarray
        """
        return self._densegp_gpu.inputs()

    @property
    def targets(self):
        """
        Returns targets for the emulator as a numpy array

        :returns: Emulator targets, 1D array with shape ``(n,)``
        :rtype: ndarray
        """
        return self._densegp_gpu.targets()

    @property
    def n(self):
        """
        Returns number of training examples for the emulator

        :returns: Number of training examples for the emulator object
        :rtype: int
        """
        return self._densegp_gpu.data_length()

    @property
    def D(self):
        """
        Returns number of inputs (dimensions) for the emulator

        :returns: Number of inputs for the emulator object
        :rtype: int
        """
        return self._densegp_gpu.D()

    @property
    def n_params(self):
        """
        Returns number of hyperparameters

        Returns the number of hyperparameters for the emulator. The number depends on the
        choice of mean function, covariance function, and nugget strategy, and possibly the
        number of inputs for certain choices of the mean function.

        :returns: Number of hyperparameters
        :rtype: int
        """
        return self._densegp_gpu.n_params()

    @property
    def nugget_type(self):
        """
        Returns method used to select nugget parameter

        Returns a string indicating how the nugget parameter is treated, either ``"adaptive"``,
        ``"fit"``, or ``"fixed"``. This is automatically set when changing the ``nugget``
        property.

        :returns: Current nugget fitting method
        :rtype: str
        """
        return self._nugget_type.__str__().split(".")[1]

    @property
    def nugget(self):
        return self._nugget

    @nugget.setter
    def nugget(self, nugget):
        if not isinstance(nugget, (str, float)):
            try:
                nugget = float(nugget)
            except TypeError:
                raise TypeError("nugget parameter must be a string or a non-negative float")

        if isinstance(nugget, str):
            if nugget == "adaptive":
                self._nugget_type = libgpgpu.nugget_type(0)
            elif nugget == "fit":
                self._nugget_type = libgpgpu.nugget_type(1)
            else:
                raise ValueError("nugget must be a float set to 'adaptive', 'fit', or 'fixed'")
            self._nugget = None
        else:
            if nugget < 0.:
                raise ValueError("nugget parameter must be non-negative")
            self._nugget_type = libgpgpu.nugget_type(2) #fixed
            self._nugget = float(nugget)

    @property
    def theta(self):
        """
        Returns emulator hyperparameters
        see
        :func:`mogp_emulator.GaussianProcess.GaussianProcess.theta`

        :type theta: ndarray
        """
        theta = np.zeros(self.n_params)
        self._densegp_gpu.get_theta(theta)
        return theta

    @theta.setter
    def theta(self, theta):
        """
        Fits the emulator and sets the parameters (property-based setter
        alias for ``fit``)

        See :func:`mogp_emulator.GaussianProcess.GaussianProcess.theta`

        :type theta: ndarray
        :returns: None
        """
        self.fit(theta)

    def get_K_matrix(self):
        """
        Returns current value of the inverse covariance matrix as a numpy array.
        Does not include the nugget
        parameter, as this is dependent on how the nugget is fit.
        """
        result = np.zeros((self.n, self.n))
        self._densegp_gpu.get_invQ(result)
        return np.linalg.inv(result)

    def fit(self, theta):
        """
        Fits the emulator and sets the parameters.

        Implements the same interface as
        :func:`mogp_emulator.GaussianProcess.GaussianProcess.fit`
        """
        theta = np.array(theta)
        self._densegp_gpu.update_theta(theta, self._nugget_type)

    def logposterior(self, theta):
        """
        Calculate the negative log-posterior at a particular value of the hyperparameters

        See :func:`mogp_emulator.GaussianProcess.GaussianProcess.logposterior`

        :param theta: Value of the hyperparameters. Must be array-like with shape ``(n_params,)``
        :type theta: ndarray
        :returns: negative log-posterior
        :rtype: float
        """
        if self.theta is None or not np.allclose(theta, self.theta, rtol=1.e-10, atol=1.e-15):
            self.fit(theta)

        return self._densegp_gpu.get_logpost()

    def logpost_deriv(self, theta):
        """
        Calculate the partial derivatives of the negative log-posterior

        See :func:`mogp_emulator.GaussianProcess.GaussianProcess.logpost_deriv`
        :param theta: Value of the hyperparameters. Must be array-like with shape
                      ``(n_params,)``
        :type theta: ndarray
        :returns: partial derivatives of the negative log-posterior with respect to the
                  hyperparameters (array with shape ``(n_params,)``)
        :rtype: ndarray
        """
        theta = np.array(theta)

        assert theta.shape == (self.n_params,), "bad shape for new parameters"

        if self.theta is None or not np.allclose(theta, self.theta, rtol=1.e-10, atol=1.e-15):
            self.fit(theta)

        result = np.zeros(self.n_params)
        self._densegp_gpu.dloglik_dtheta(result)
        return result

    def logpost_hessian(self, theta):
        """
        Calculate the Hessian of the negative log-posterior

        See :func:`mogp_emulator.GaussianProcess.GaussianProcess.logpost_hessian`

        :param theta: Value of the hyperparameters. Must be array-like with shape
                      ``(n_params,)``
        :type theta: ndarray
        :returns: Hessian of the negative log-posterior (array with shape
                  ``(n_params, n_params)``)
        :rtype: ndarray
        """
        pass

    def predict(self, testing, unc=True, deriv=False, include_nugget=False):
        """
        Make a prediction for a set of input vectors for a single set of hyperparameters.
        This method implements the same interface as
        :func:`mogp_emulator.GaussianProcess.GaussianProcess.predict`
        """
#        if self.theta is None:
 #           raise ValueError("hyperparameters have not been fit for this Gaussian Process")

        testing = np.array(testing)
        if testing.ndim == 1:
            testing = np.reshape(testing, (1, len(testing)))
        assert testing.ndim == 2

        means = np.zeros(testing.shape[0])
        variances = np.zeros(testing.shape[0])
        deriv = np.zeros(testing.shape[0])
        if unc:
            self._densegp_gpu.predict_variance_batch(testing, means, variances)
        else:
            self._densegp_gpu.predict_batch(testing, means)
        return PredictResult(mean=means, unc=variances, deriv=deriv)


    def __call__(self, testing):
        """A Gaussian process object is callable: calling it is the same as
        calling `predict` without uncertainty and derivative
        predictions, and extracting the zeroth component for the
        'mean' prediction.
        """
        return (self.predict(testing, unc=False, deriv=False)[0])


    def __str__(self):
        """
        Returns a string representation of the model

        :returns: A string representation of the model
        (indicates number of training examples and inputs)
        :rtype: str
        """
        return ("Gaussian Process with " + str(self.n) + " training examples and " +
                str(self.D) + " input variables")
