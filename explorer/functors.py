from __future__ import print_function, division

import pandas as pd
import numpy as np
import re
import fastparquet
import logging
import dask.dataframe as dd
import dask.array as da
import dask
import time
import inspect

from .utils import result

class Functor(object):
    """Base class for computations performed on catalogs

    Subclasses must implement `_func`, which defines the calculation
    to be performed.  The way the calculation actually happens is an 
    interaction between the catalog's `_apply_func` method
    (which uses the functor's `_func` method and defines the format
    and content of the result and such) 
    and the functor `__call__` method, which controls things like
    whether nans are dropped.

    Subclasses must also define `_columns` attribute (or equivalently
    a `columns` property) that defines which
    columns are required to be read from the catalog to perform the calculation.

    Results are returned by default as in-memory `pandas.Series` or `pandas.DataFrame`, 
    unless called with `dask=True`, in which case a dask DataFrame is returned.

    Note that when implementing `_func`, use the dask versions of ufuncs for 
    array math, instead of numpy; e.g. `da.sin`, `da.cos`, 
    instead of `np.sin`, `np.cos`.


    Parameters
    ----------
    allow_difference : bool
        Defines whether to allow operations like subtraction of this calculation
        performed on two different catalogs (e.g., should be False for labels)
    """
    _allow_difference = True


    def __init__(self, allow_difference=None):
        if allow_difference is not None:
            self.allow_difference = allow_difference
        else:
            self.allow_difference = self._allow_difference

    @property
    def columns(self):
        """Columns required to perform calculation
        """
        try:
            return self._columns
        except AttributeError:
            raise NotImplementedError('Must define columns property or _columns attribute')

    def _func(self, df, dropna=True):
        raise NotImplementedError('Must define calculation on dataframe')

    def __call__(self, catalog, client=None, query=None, dropna=True, dask=False, 
                flags=None, **kwargs):

        if isinstance(catalog, dd.DataFrame):
            vals = self._func(catalog)
        else:
            vals = catalog._apply_func(self, query=query, client=client, **kwargs)

        # dropna=True can be buggy; e.g. for boolean or string types perhaps?
        if dropna:
            try:
                if client is not None:
                    vals = client.compute(vals[da.isfinite(vals)])
                else:
                    vals = vals[da.isfinite(vals)]
            except (TypeError, ValueError):
                if client is not None:
                    vals = client.compute(vals.dropna())
                else:
                    vals = vals.dropna()

            # try:
            #     if catalog.client:
            #         vals = catalog.client.compute(vals[da.isfinite(vals)]).result()
            #     else:
            #         vals = vals[da.isfinite(vals)]
            # except TypeError:
            #     if catalog.client:
            #         vals = catalog.client.compute(vals[da.notnull(vals)]).result()
            #     else:
            #         vals = vals[da.notnull(vals)]
            # except AttributeError:
            #     vals = vals[da.notnull(vals)]

        if dask:
            return vals
        else:
            return result(vals)

    def test(self, catalog, dropna=True, dask=False):
        """Time how long the calculation takes on a catalog.
        """
        start = time.time()
        res = self(catalog, dropna=dropna, dask=dask)
        n = len(result(res))
        end = time.time()
        
        runtime = end - start
        print('Test results for {}:'.format(self.name))
        print('  Took {:.2f}s, length={}.  Type={}'.format(runtime, n, type(res)))    
        return runtime

class ParquetReadWorker(object):
    def __init__(self, cols):
        self.cols = cols

    def __call__(self, filename):
        pfile = fastparquet.ParquetFile(filename)
        return pfile.to_pandas(columns=self.cols)

class func_worker(object):
    def __init__(self, catalog):
        self.catalog = catalog

    def __call__(self, func):
        return func(self.catalog)

# def get_cols(catalog, composite_functor):
#     worker = func_worker(catalog)
#     return catalog.client.map(worker, composite_functor.funcDict.values())

class CompositeFunctor(Functor):
    """Perform multiple calculations at once on a catalog

    Returns a dataframe with columns being the keys of `funcDict`.  If called
    on a `MatchedCatalog` or `MultiMatchedCatalog` with `how='all'`, then 
    the columns will be a multi-level index, indexed first by functor name
    then by catalog name.
    
    Parameters
    ----------
    funcDict : dict
        Dictionary of functors.  

    dask : bool (default False)
        Whether to return result as a dask DataFrame.  Not much used (or tested)

    do_map : bool
        If `client` is provided, whether to map with the client over the multiple 
        functors.  

    client : distributed.Client
        If provided, then computation will map over functors if `do_map` is True.
    """
    force_ndarray = False

    def __init__(self, funcDict, **kwargs):
        self.funcDict = funcDict
        super(CompositeFunctor, self).__init__(**kwargs)

    @property 
    def columns(self):
        return [x for y in [f.columns for f in self.funcDict.values()] for x in y]

    def __call__(self, catalog, dask=False, do_map=False, client=None, **kwargs):
        if client is not None and do_map:
            worker = func_worker(catalog)
            cols = client.map(worker, self.funcDict.values())
            # cols = get_cols(catalog, self)
            df = pd.concat({k:result(c) for k,c in zip(self.funcDict.keys(), cols)}, axis=1)
        else:
            df = pd.concat({k : f(catalog, dask=dask, client=client, **kwargs) 
                            for k,f in self.funcDict.items()}, axis=1)

        if dask:
            return dd.from_pandas(df, chunksize=1000000)
        else:
            return df

    def __getitem__(self, item):
        return self.funcDict[item]

class TestFunctor(Functor):
    name = 'test'

    def __init__(self, n=None, seed=1234, **kwargs):
        self.n = n
        self.seed = seed
        super(TestFunctor, self).__init__(**kwargs)

    def __call__(self, catalog):
        np.random.seed(self.seed)
        n = len(catalog) if self.n is None else self.n
        u = np.random.random(n)
        x = np.ones(n)
        x[u < 0.5] = -1
        return x

def mag_aware_eval(df, expr):
    """Evaluate an expression on a DataFrame, knowing what the 'mag' function means

    Builds on `pandas.DataFrame.eval`, which parses and executes math on dataframes.

    Parameters
    ----------
    df : pandas.DataFrame
        Dataframe on which to evaluate expression.

    expr : str
        Expression.
    """
    try:
        expr_new = re.sub('mag\((\w+)\)', '-2.5*log(\g<1>)/log(10)', expr)
        val = df.eval(expr_new, truediv=True)
    except:
        expr_new = re.sub('mag\((\w+)\)', '-2.5*log(\g<1>_flux)/log(10)', expr)
        val = df.eval(expr_new, truediv=True)
    return val

class CustomFunctor(Functor):
    """Arbitrary computation on a catalog

    Column names (and thus the columns to be loaded from catalog) are found
    by finding all words and trying to ignore all "math-y" words.

    Parameters
    ----------
    expr : str
        Expression to evaluate, to be parsed and executed by `mag_aware_eval`.
    """
    _ignore_words = ('mag', 'sin', 'cos', 'exp', 'log', 'sqrt')

    def __init__(self, expr, **kwargs):
        self.expr = expr
        super(CustomFunctor, self).__init__(**kwargs)

    @property
    def name(self):
        return self.expr

    @property
    def columns(self):
        flux_cols = re.findall('mag\(\s*(\w+)\s*\)', self.expr)

        cols = [c for c in re.findall('[a-zA-Z_]+', self.expr) if c not in self._ignore_words]
        not_a_col = []
        for c in flux_cols:
            if not re.search('_flux$', c):
                cols.append('{}_flux'.format(c))
                not_a_col.append(c)
            else:
                cols.append(c)

        return list(set([c for c in cols if c not in not_a_col]))

    def _func(self, df):
        return mag_aware_eval(df, self.expr)

class Column(Functor):
    """Get column with specified name
    """
    def __init__(self, col, **kwargs):
        self.col = col
        super(Column, self).__init__(**kwargs)

    @property
    def name(self):
        return self.col

    @property
    def columns(self):
        return [self.col]

    def _func(self, df):
        return df[self.col]

class IDColumn(Column):
    col = 'id'
    _allow_difference = False

class FootprintNPix(Column):
    col = 'base_Footprint_nPix'

class CoordColumn(Column):
    """Base class for coordinate column
    """
    _allow_difference = False
    _radians = True

    def __init__(self, col, calculate=False, **kwargs):
        self.calculate = calculate
        super(CoordColumn, self).__init__(col, allow_difference=calculate, **kwargs)

    def _func(self, df):
        res = df[self.col]
        if self._radians:
            res *= 180 / np.pi
        return res

class RAColumn(CoordColumn):
    name = 'RA'
    def __init__(self, **kwargs):
        super(RAColumn, self).__init__('coord_ra', **kwargs)

    def __call__(self, catalog, **kwargs):
        if kwargs.pop('calculate', False) or self.calculate:
            return super(RAColumn, self).__call__(catalog, **kwargs)
        else:
            return catalog.ra

class DecColumn(CoordColumn):
    name = 'Dec'
    def __init__(self, **kwargs):
        super(DecColumn, self).__init__('coord_dec', **kwargs)

    def __call__(self, catalog, **kwargs):
        if kwargs.pop('calculate', False) or self.calculate:
            return super(DecColumn, self).__call__(catalog, **kwargs)
        else:
            return catalog.dec


def fluxName(col):
    if not col.endswith('_flux'):
        col += '_flux'
    return col

class Mag(Functor):
    def __init__(self, col, **kwargs):
        self.col = fluxName(col)
        super(Mag, self).__init__(**kwargs)

    @property
    def columns(self):
        return [self.col]

    def _func(self, df):
        return -2.5*da.log10(df[self.col])

    @property
    def name(self):
        return 'mag_{0}'.format(self.col)

class MagDiff(Functor):
    """Functor to calculate magnitude difference"""
    def __init__(self, col1, col2, **kwargs):
        self.col1 = fluxName(col1)
        self.col2 = fluxName(col2)
        super(MagDiff, self).__init__(**kwargs)

    @property
    def columns(self):
        return [self.col1, self.col2]

    def _func(self, df):
        return -2.5*da.log10(df[self.col1]/df[self.col2])

    @property
    def name(self):
        return '(mag_{0} - mag_{1})'.format(self.col1, self.col2)

class Labeller(Functor):
    """Main function of this subclass is to override the dropna=True
    """
    _null_label = 'null'
    _allow_difference = False
    name = 'label'
    _force_str = False

    def __call__(self, catalog, dropna=False, **kwargs):
        return super(Labeller, self).__call__(catalog, dropna=False, **kwargs)

class StarGalaxyLabeller(Labeller):
    _columns = ["base_ClassificationExtendedness_value"]
    _column = "base_ClassificationExtendedness_value"

    def _func(self, df):
        x = df[self._columns][self._column]
        mask = x.isnull()
        test = (x < 0.5).astype(int)
        test = test.mask(mask, 2)
        #are these backwards?
        label = pd.Series(pd.Categorical.from_codes(test, categories=['galaxy', 'star', self._null_label]), 
                            index=x.index, name='label')
        if self._force_str:
            label = label.astype(str)
        return label
        # return np.where(df[self._column] < 0.5, 'star', 'galaxy')

class NumStarLabeller(Labeller):
    _columns = ['numStarFlags']
    labels = {"star": 0, "maybe": 1, "notStar": 2}

    def _func(self, df):
        x = df[self._columns][self._columns[0]]

        # Number of filters
        n = len(x.unique()) - 1 

        label = pd.Series(pd.cut(x, [-1, 0, n-1 , n], labels=['noStar', 'maybe', 'star']),
                            index=x.index, name='label')

        if self._force_str:
            label = label.astype(str)

        return label


class DeconvolvedMoments(Functor):
    name = 'Deconvolved Moments'
    _columns = ("ext_shapeHSM_HsmSourceMoments_xx",
                "ext_shapeHSM_HsmSourceMoments_yy",
                "base_SdssShape_xx", "base_SdssShape_yy",
                # "ext_shapeHSM_HsmSourceMoments",
                "ext_shapeHSM_HsmPsfMoments_xx",
                "ext_shapeHSM_HsmPsfMoments_yy")

    def _func(self, df):
        """Calculate deconvolved moments"""
        if "ext_shapeHSM_HsmSourceMoments_xx" in df.columns: # _xx added by tdm
            hsm = df["ext_shapeHSM_HsmSourceMoments_xx"] + df["ext_shapeHSM_HsmSourceMoments_yy"]
        else:
            hsm = np.ones(len(df))*np.nan
        sdss = df["base_SdssShape_xx"] + df["base_SdssShape_yy"]
        if "ext_shapeHSM_HsmPsfMoments_xx" in df.columns:
            psf = df["ext_shapeHSM_HsmPsfMoments_xx"] + df["ext_shapeHSM_HsmPsfMoments_yy"]
        else:
            # LSST does not have shape.sdss.psf.  Could instead add base_PsfShape to catalog using
            # exposure.getPsf().computeShape(s.getCentroid()).getIxx()
            # raise TaskError("No psf shape parameter found in catalog")
            raise RuntimeError('No psf shape parameter found in catalog')

        return hsm.where(da.isfinite(hsm), sdss) - psf

class SdssTraceSize(Functor):
    """Functor to calculate SDSS trace radius size for sources"""
    name = "SDSS Trace Size"
    _columns = ("base_SdssShape_xx", "base_SdssShape_yy")

    def _func(self, df):
        srcSize = da.sqrt(0.5*(df["base_SdssShape_xx"] + df["base_SdssShape_yy"]))
        return srcSize


class PsfSdssTraceSizeDiff(Functor):
    """Functor to calculate SDSS trace radius size difference (%) between object and psf model"""
    name = "PSF - SDSS Trace Size"
    _columns = ("base_SdssShape_xx", "base_SdssShape_yy",
                "base_SdssShape_psf_xx", "base_SdssShape_psf_yy")

    def _func(self, df):
        srcSize = da.sqrt(0.5*(df["base_SdssShape_xx"] + df["base_SdssShape_yy"]))
        psfSize = da.sqrt(0.5*(df["base_SdssShape_psf_xx"] + df["base_SdssShape_psf_yy"]))
        sizeDiff = 100*(srcSize - psfSize)/(0.5*(srcSize + psfSize))
        return sizeDiff


class HsmTraceSize(Functor):
    """Functor to calculate HSM trace radius size for sources"""
    name = 'HSM Trace Size'
    _columns = ("ext_shapeHSM_HsmSourceMoments_xx",
                "ext_shapeHSM_HsmSourceMoments_yy")
    def _func(self, df):
        srcSize = da.sqrt(0.5*(df["ext_shapeHSM_HsmSourceMoments_xx"] +
                               df["ext_shapeHSM_HsmSourceMoments_yy"]))
        return srcSize


class PsfHsmTraceSizeDiff(Functor):
    """Functor to calculate HSM trace radius size difference (%) between object and psf model"""
    name = 'PSF - HSM Trace Size'
    _columns = ("ext_shapeHSM_HsmSourceMoments_xx",
                "ext_shapeHSM_HsmSourceMoments_yy",
                "ext_shapeHSM_HsmPsfMoments_xx",
                "ext_shapeHSM_HsmPsfMoments_yy")

    def _func(self, df):
        srcSize = da.sqrt(0.5*(df["ext_shapeHSM_HsmSourceMoments_xx"] +
                               df["ext_shapeHSM_HsmSourceMoments_yy"]))
        psfSize = da.sqrt(0.5*(df["ext_shapeHSM_HsmPsfMoments_xx"] +
                               df["ext_shapeHSM_HsmPsfMoments_yy"]))
        sizeDiff = 100*(srcSize - psfSize)/(0.5*(srcSize + psfSize))
        return sizeDiff

class Seeing(Functor):
    name = 'seeing'
    _columns = ('ext_shapeHSM_HsmPsfMoments_xx', 'ext_shapeHSM_HsmPsfMoments_yy')

    def _func(self, df):
        return 0.168*2.35*da.sqrt(0.5*(df['ext_shapeHSM_HsmPsfMoments_xx']**2 +
                                       df['ext_shapeHSM_HsmPsfMoments_yy']**2))
