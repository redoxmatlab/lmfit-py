"""
Concise nonlinear curve fitting.
"""

import warnings
import inspect
from copy import deepcopy
import numpy as np
from . import Parameters, Parameter, Minimizer
from .printfuncs import fit_report

# Use pandas.isnull for aligning missing data is pandas is available.
# otherwise use numpy.isnan
try:
    from pandas import isnull, Series
except ImportError:
    isnull = np.isnan
    Series = type(NotImplemented)

def _align(var, mask, data):
    "align missing data, with pandas is available"
    if isinstance(data, Series) and isinstance(var, Series):
        return var.reindex_like(data).dropna()
    elif mask is not None:
        return var[mask]
    return var

class Model(object):
    """Create a model from a user-defined function.

    Parameters
    ----------
    func: function to be wrapped
    independent_vars: list of strings or None (default)
        arguments to func that are independent variables
    param_names: list of strings or None (default)
        names of arguments to func that are to be made into parameters
    missing: None, 'none', 'drop', or 'raise'
        'none' or None: Do not check for null or missing values (default)
        'drop': Drop null or missing observations in data.
            if pandas is installed, pandas.isnull is used, otherwise
            numpy.isnan is used.
        'raise': Raise a (more helpful) exception when data contains null
            or missing values.
    name: None or string
        name for the model. When `None` (default) the name is the same as
        the model function (`func`).

    Note
    ----
    Parameter names are inferred from the function arguments,
    and a residual function is automatically constructed.

    Example
    -------
    >>> def decay(t, tau, N):
    ...     return N*np.exp(-t/tau)
    ...
    >>> my_model = Model(decay, independent_vars=['t'])
    """

    _forbidden_args = ('data', 'weights', 'params')
    _invalid_ivar  = "Invalid independent variable name ('%s') for function %s"
    _invalid_par   = "Invalid parameter name ('%s') for function %s"
    _invalid_missing = "missing must be None, 'none', 'drop', or 'raise'."
    _names_collide = "Two models have parameters named %s. Use distinct names"

    def __init__(self, func, independent_vars=None, param_names=None,
                 missing='none', prefix='', name=None, components=None, **kws):
        self.func = func
        self.prefix = prefix
        self.param_names = param_names
        self.independent_vars = independent_vars
        if components is None:
            self.components = []
        self.func_allargs = []
        self.func_haskeywords = False
        if not missing in [None, 'none', 'drop', 'raise']:
            raise ValueError(self._invalid_missing)
        self.missing = missing
        self.opts = kws
        self.result = None
        self.param_hints = {}
        self._parse_params()
        if self.independent_vars is None:
            self.independent_vars = []
        if name is None and hasattr(self.func, '__name__'):
            name = self.func.__name__
        self._name = name

    def _reprstring(self, long=False):
        if not self.is_composite():
            # base model
            opts = []
            if len(self.prefix) > 0:
                opts.append("prefix='%s'" % (self.prefix))
            if long:
                for k, v in self.opts.items():
                    opts.append("%s='%s'" % (k, v))

            out = ["%s" % self._name]
            if len(opts) > 0:
                out[0] = "%s(%s)" % (out[0], ','.join(opts))
        else:
            # composite model
            if self._name is None:
                out = [c._reprstring(long)[0] for c in self.components]
            else:
                out = [self._name]
        return out

    @property
    def name(self):
        return '+'.join(self._reprstring(long=False))

    @name.setter
    def name(self, value):
        self._name = value

    def is_composite(self):
        return len(self.components) > 0

    def __repr__(self):
        return  "<lmfit.Model: %s>" % (self.name)

    def _parse_params(self):
        "build params from function arguments"
        if self.func is None:
            return
        argspec = inspect.getargspec(self.func)
        pos_args = argspec.args[:]
        keywords = argspec.keywords
        kw_args = {}
        if argspec.defaults is not None:
            for val in reversed(argspec.defaults):
                kw_args[pos_args.pop()] = val
        #
        self.func_haskeywords = keywords is not None
        self.func_allargs = pos_args + list(kw_args.keys())
        allargs = self.func_allargs

        if len(allargs) == 0 and keywords is not None:
            return

        # default independent_var = 1st argument
        if self.independent_vars is None:
            self.independent_vars = [pos_args[0]]

        # default param names: all positional args
        # except independent variables
        self.def_vals = {}
        might_be_param = []
        if self.param_names is None:
            self.param_names = pos_args[:]
            for key, val in kw_args.items():
                if (not isinstance(val, bool) and
                    isinstance(val, (float, int))):
                    self.param_names.append(key)
                    self.def_vals[key] = val
                elif val is None:
                    might_be_param.append(key)
            for p in self.independent_vars:
                if p in self.param_names:
                    self.param_names.remove(p)

        new_opts = {}
        for opt, val in self.opts.items():
            if (opt in self.param_names or opt in might_be_param and
                isinstance(val, Parameter)):
                self.set_param_hint(opt, value=val.value,
                                    min=val.min, max=val.max, expr=val.expr)
            elif opt in self.func_allargs:
                new_opts[opt] = val
        self.opts = new_opts

        # check variables names for validity
        # The implicit magic in fit() requires us to disallow some
        fname = self.func.__name__
        for arg in self.independent_vars:
            if arg not in allargs or arg in self._forbidden_args:
                raise ValueError(self._invalid_ivar % (arg, fname))
        for arg in self.param_names:
            if arg not in allargs or arg in self._forbidden_args:
                raise ValueError(self._invalid_par % (arg, fname))

        names = []
        if self.prefix is None:
            self.prefix = ''
        for pname in self.param_names:
            names.append("%s%s" % (self.prefix, pname))
        self.param_names = set(names)

    def set_param_hint(self, name, value=None,
                       min=None, max=None, expr=None):
        """set hints for parameter, including optional bounds and
        constraints  (value, min, max, expr)
        these will be used by make_params() when building
        default  parameters
        """
        npref = len(self.prefix)
        if npref > 0 and name.startswith(self.prefix):
            name = name[npref:]

        if name not in self.param_hints:
            self.param_hints[name] = {}
        hints = self.param_hints[name]
        if value is not None:
            hints['value'] = value
        if expr is not None:
            hints['expr'] = expr
        if min not in (None, -np.inf):
            hints['min'] = min
        if max not in (None, np.inf):
            hints['max'] = max

    def make_params(self, **kwargs):
        """create and return a Parameters object for a Model.
        This applies any default values
        """
        params = Parameters()
        if not self.is_composite():
            # base model: build Parameters from scratch
            for name in self.param_names:
                par = Parameter(name=name)
                basename = name[len(self.prefix):]
                # apply defaults from model function definition
                if basename in self.def_vals:
                    par.value = self.def_vals[basename]
                # apply defaults from parameter hints
                if basename in self.param_hints:
                    hint = self.param_hints[basename]
                    for item in ('value', 'min', 'max', 'expr'):
                        if item in  hint:
                            setattr(par, item, hint[item])
                # apply values passed in through kw args
                if basename in kwargs:
                    # kw parameter names with no prefix
                    par.value = kwargs[basename]
                if name in kwargs:
                    # kw parameter names with prefix
                    par.value = kwargs[name]
                params[name] = par
        else:
            # composite model: merge the sub_models parameters adding hints
            for sub_model in self.components:
                comp_params = sub_model.make_params(**kwargs)
                for par_name, param in comp_params.items():
                    # apply composite-model hints
                    if par_name in self.param_hints:
                        hint = self.param_hints[par_name]
                        for item in ('value', 'min', 'max', 'expr'):
                            if item in  hint:
                                setattr(param, item, hint[item])
                params.update(comp_params)

            # apply defaults passed in through kw args
            for name in self.param_names:
                if name in kwargs:
                    params[name].value = kwargs[name]

        # In both cases (base or composite) add new parameters that have hints
        for basename, hint in self.param_hints.items():
            name = "%s%s" % (self.prefix, basename)
            if name not in params:
                par = params[name] = Parameter(name=name)
                for item in ('value', 'min', 'max', 'expr'):
                    if item in  hint:
                        setattr(par, item, hint[item])
                # Add the new parameter to the self.param_names
                self.param_names.add(name)
        return params

    def guess(self, data=None, **kws):
        """stub for guess starting values --
        should be implemented for each model subclass to
        run self.make_params(), update starting values
        and return a Parameters object"""
        cname = self.__class__.__name__
        msg = 'guess() not implemented for %s' % cname
        raise NotImplementedError(msg)

    def _residual(self, params, data, weights, **kwargs):
        "default residual:  (data-model)*weights"
        diff = self.eval(params, **kwargs) - data
        if weights is not None:
            diff *= weights
        return np.asarray(diff)  # for compatibility with pandas.Series

    def _handle_missing(self, data):
        "handle missing data"
        if self.missing == 'raise':
            if np.any(isnull(data)):
                raise ValueError("Data contains a null value.")
        elif self.missing == 'drop':
            mask = ~isnull(data)
            if np.all(mask):
                return None  # short-circuit this -- no missing values
            mask = np.asarray(mask)  # for compatibility with pandas.Series
            return mask

    def make_funcargs(self, params=None, kwargs=None, strip=True):
        """convert parameter values and keywords to function arguments"""
        if params is None: params = {}
        if kwargs is None: kwargs = {}
        out = {}
        out.update(self.opts)
        npref = len(self.prefix)
        def strip_prefix(name):
            if strip and npref > 0 and name.startswith(self.prefix):
                name = name[npref:]
            return name
        for name, par in params.items():
            name = strip_prefix(name)
            if name in self.func_allargs or self.func_haskeywords:
                out[name] = par.value

        # kwargs handled slightly differently -- may set param value too!
        for name, val in kwargs.items():
            name = strip_prefix(name)
            if name in self.func_allargs or self.func_haskeywords:
                out[name] = val
                if name in params:
                    params[name].value = val
        return out

    def _make_all_args(self, params=None, **kwargs):
        """generate **all** function args for all functions"""
        args = {}
        for key, val in self.make_funcargs(params, kwargs).items():
            args["%s%s" % (self.prefix, key)] = val
        for sub_model in self.components:
            otherargs = sub_model._make_all_args(params, **kwargs)
            args.update(otherargs)
        return args

    def eval(self, params=None, **kwargs):
        """evaluate the model with the supplied parameters"""
        if not self.is_composite():
            fcnargs = self.make_funcargs(params, kwargs)
            result = self.func(**fcnargs)
        else:
            for i, sub_model in enumerate(self.components):
                if i == 0:
                    result = sub_model.eval(params, **kwargs)
                else:
                    result += sub_model.eval(params, **kwargs)
        return result

    def fit(self, data, params=None, weights=None, method='leastsq',
            iter_cb=None, scale_covar=True, **kwargs):
        """Fit the model to the data.

        Parameters
        ----------
        data: array-like
        params: Parameters object
        weights: array-like of same size as data
            used for weighted fit
        method: fitting method to use (default = 'leastsq')
        iter_cb:  None or callable  callback function to call at each iteration.
        scale_covar:  bool (default True) whether to auto-scale covariance matrix
        keyword arguments: optional, named like the arguments of the
            model function, will override params. See examples below.

        Returns
        -------
        lmfit.ModelFitResult

        Examples
        --------
        # Take t to be the independent variable and data to be the
        # curve we will fit.

        # Using keyword arguments to set initial guesses
        >>> result = my_model.fit(data, tau=5, N=3, t=t)

        # Or, for more control, pass a Parameters object.
        >>> result = my_model.fit(data, params, t=t)

        # Keyword arguments override Parameters.
        >>> result = my_model.fit(data, params, tau=5, t=t)

        Note
        ----
        All parameters, however passed, are copied on input, so the original
        Parameter objects are unchanged.

        """
        if params is None:
            params = self.make_params()
        else:
            params = deepcopy(params)

        # If any kwargs match parameter names, override params.
        param_kwargs = set(kwargs.keys()) & self.param_names
        for name in param_kwargs:
            p = kwargs[name]
            if isinstance(p, Parameter):
                p.name = name  # allows N=Parameter(value=5) with implicit name
                params[name] = deepcopy(p)
            else:
                params[name].set(value=p)
            del kwargs[name]

        # All remaining kwargs should correspond to independent variables.
        for name in kwargs.keys():
            if not name in self.independent_vars:
                warnings.warn("The keyword argument %s does not" % name +
                              "match any arguments of the model function." +
                              "It will be ignored.", UserWarning)

        # If any parameter is not initialized raise a more helpful error.
        missing_param = any([p not in params.keys()
                             for p in self.param_names])
        blank_param = any([(p.value is None and p.expr is None)
                           for p in params.values()])
        if missing_param or blank_param:
            raise ValueError("""Assign each parameter an initial value by
 passing Parameters or keyword arguments to fit""")

        # Handle null/missing values.
        mask = None
        if self.missing not in (None, 'none'):
            mask = self._handle_missing(data)  # This can raise.
            if mask is not None:
                data = data[mask]
            if weights is not None:
                weights = _align(weights, mask, data)

        # If independent_vars and data are alignable (pandas), align them,
        # and apply the mask from above if there is one.
        for var in self.independent_vars:
            if not np.isscalar(self.independent_vars):  # just in case
                kwargs[var] = _align(kwargs[var], mask, data)

        output = ModelFitResult(self, params, method=method, iter_cb=iter_cb,
                                scale_covar=scale_covar, fcn_kws=kwargs)
        output.fit(data=data, weight=weights)
        return output

    def __add__(self, other):
        colliding_param_names = self.param_names & other.param_names
        if len(colliding_param_names) != 0:
            collision = colliding_param_names.pop()
            raise NameError(self._names_collide % collision)

        if self.is_composite():
            # If the model is already composite just make a copy
            new = deepcopy(self)
        else:
            # otherwise create a new Model and put self in components
            new = Model(func=None)
            new.components.append(self)
            new.param_names = set(self.param_names)
            # we assume that all the sub-models have the same independent vars
            new.independent_vars = self.independent_vars[:]  # copy
        new.components.append(other)
        new.param_names |= other.param_names
        return new


class ModelFitResult(Minimizer):
    """Result from Model fit

    Attributes
    -----------
    model         instance of Model -- the model function
    params        instance of Parameters -- the fit parameters
    data          array of data values to compare to model
    weights       array of weights used in fitting
    init_params   copy of params, before being updated by fit()
    init_values   array of parameter values, before being updated by fit()
    init_fit      model evaluated with init_params.
    best_fit      model evaluated with params after being updated by fit()

    Methods:
    --------
    fit(data=None, params=None, weights=None, method=None, **kwargs)
         fit (or re-fit) model with params to data (with weights)
         using supplied method.  The keyword arguments are sent to
         as keyword arguments to the model function.

         all inputs are optional, defaulting to the value used in
         the previous fit.  This allows easily changing data or
         parameter settings, or both.

    eval(**kwargs)
         evaluate the current model, with the current parameter values,
         with values in kwargs sent to the model function.

   fit_report(modelpars=None, show_correl=True, min_correl=0.1)
         return a fit report.

    """
    def __init__(self, model, params, data=None, weights=None,
                 method='leastsq', fcn_args=None, fcn_kws=None,
                 iter_cb=None, scale_covar=True, **fit_kws):
        self.model = model
        self.data = data
        self.weights = weights
        self.method = method
        self.init_params = deepcopy(params)
        Minimizer.__init__(self, model._residual, params, fcn_args=fcn_args,
                           fcn_kws=fcn_kws, iter_cb=iter_cb,
                           scale_covar=scale_covar, **fit_kws)

    def fit(self, data=None, params=None, weights=None, method=None, **kwargs):
        """perform fit for a Model, given data and params"""
        if data is not None:
            self.data = data
        if params is not None:
            self.params = params
        if weights is not None:
            self.weights = weights
        if method is not None:
            self.method = method
        self.userargs = (self.data, self.weights)
        self.userkws.update(kwargs)
        self.init_params = deepcopy(self.params)
        self.init_values = self.model._make_all_args(self.init_params)
        self.init_fit    = self.model.eval(params=self.init_params, **self.userkws)

        self.minimize(method=self.method)
        self.best_fit = self.model.eval(params=self.params, **self.userkws)
        self.best_values = self.model._make_all_args(self.params)

    def eval(self, **kwargs):
        self.userkws.update(kwargs)
        return self.model.eval(params=self.params, **self.userkws)

    def fit_report(self, modelpars=None, show_correl=True, min_correl=0.1):
        "return fit report"
        stats_report = fit_report(self, modelpars=modelpars,
                                 show_correl=show_correl,
                                 min_correl=min_correl)
        buff = ['[[Model]]']
        for x in self.model._reprstring(long=True):
            buff.append('    %s' % x)
        buff = '\n'.join(buff)
        out = '%s\n%s' % (buff, stats_report)
        return out
