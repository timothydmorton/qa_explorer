import numpy as np
import pandas as pd
import holoviews as hv

from .functors import Functor, CompositeFunctor, Column, RAColumn, DecColumn, Mag
from .functors import StarGalaxyLabeller
from .catalog import MatchedCatalog, MultiMatchedCatalog

class QADataset(object):
    def __init__(self, catalog, funcs, flags=None, 
                 xFunc=Mag('base_PsfFlux', allow_difference=False), 
                 labeller=StarGalaxyLabeller(),
                 query=None, client=None):

        self._set_catalog(catalog)
        self._set_funcs(funcs, xFunc, labeller)
        self._set_flags(flags)

        self.client = client

        self._df = None
        self._ds = None
        self._query = query

    def _set_catalog(self, catalog):
        self.catalog = catalog

    def _set_funcs(self, funcs, xFunc, labeller):
        if isinstance(funcs, list) or isinstance(funcs, tuple):
            self.funcs = {'y{}'.format(i):f for i,f in enumerate(funcs)}
        elif isinstance(funcs, Functor):
            self.funcs = {'y0':funcs}
        else:
            self.funcs = funcs

        self.xFunc = xFunc
        self.labeller = labeller

    def _set_flags(self, flags):
        if flags is None:
            self.flags = []
        else:
            self.flags = flags # TODO: check to make sure flags are valid                    

    def _reset(self):
        self._df = None
        self._ds = None

    @property
    def query(self):
        return self._query

    @query.setter
    def query(self, new):
        self._query = new
        self._reset()

    @property
    def allfuncs(self):
        allfuncs = self.funcs.copy()

        # Set coordinates and x value
        allfuncs.update({'ra':RAColumn(), 'dec': DecColumn(), 
                         'x':self.xFunc})

        # Include flags
        allfuncs.update({f:Column(f) for f in self.flags})

        if self.labeller is not None:
            allfuncs.update({'label':self.labeller})

        return allfuncs        

    @property
    def df(self):
        if self._df is None:
            self._make_df()
        return self._df

    @property
    def is_matched(self):
        return isinstance(self.catalog, MatchedCatalog)

    @property
    def is_multi_matched(self):
        return isinstance(self.catalog, MultiMatchedCatalog)

    def _make_df(self, **kwargs):
        f = CompositeFunctor(self.allfuncs)
        if self.is_multi_matched:
            kwargs.update(how='all')
        df = f(self.catalog, query=self.query, client=self.client, dropna=False, **kwargs)
        if self.is_matched:
            df = pd.concat([df, self.catalog.match_distance], axis=1)
        if not self.is_matched: 
            df = df.dropna(how='any')
        ids = df.index

        # if self.is_matched:
        #     flags, _ = self.catalog.get_columns(self.flags)
        # else:
        #     flags = self.catalog.get_columns(self.flags)
        # flags = flags.compute().loc[ids]
        # df = df.join(flags)
        self._df = df        

    @property
    def ds(self):
        if self._ds is None:
            self._make_ds()
        return self._ds

    def _make_ds(self):
        kdims = ['ra', 'dec', hv.Dimension('x', label=self.xFunc.name), 'label']
        kdims += self.flags
        vdims = []
        for k,v in self.allfuncs.items():
            if k in ('ra', 'dec', 'x', 'label'):
                continue
            label = v.name
            if v.allow_difference:
                if self.is_multi_matched:
                    label = 'std({})'.format(label)
                elif self.is_matched:
                    label = 'diff({})'.format(label)
            vdims.append(hv.Dimension(k, label=label))

        if self.is_matched:
            vdims += [hv.Dimension('match_distance', label='Match Distance [arcsec]')]
        ds = hv.Dataset(self.df, kdims=kdims, vdims=vdims)
        self._ds = ds        

