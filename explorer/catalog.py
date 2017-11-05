import numpy as np
import pandas as pd
import dask.dataframe as dd
from distributed import Future
import fastparquet
import glob, re
import logging

from .match import match_lists

class Catalog(object):
    index_column = 'id'

    def __init__(self, data):
        self.data
        self.columns = data.columns

        self._coords = None

    def _sanitize_columns(self, columns):
        bad_cols = [c for c in columns if c not in self.columns]
        if bad_cols:
            logging.warning('Columns not available: {}'.format(bad_cols))
        return list(set(columns) - set(bad_cols))

    def get_columns(self, columns, check_columns=True, query=None):
        if check_columns:
            columns = self._sanitize_columns(columns)
        return self.data[columns]

    def _get_coords(self):
        df = self.get_columns(['coord_ra', 'coord_dec'], add_flags=False)

        # Hack to avoid phantom 'dir0' column 
        df = df.compute()
        if 'dir0' in df.columns:
            df = df.drop('dir0', axis=1)

        self._coords = (df*180 / np.pi).rename(columns={'coord_ra':'ra',
                                                        'coord_dec':'dec'})

    @property
    def df_all(self):
        return self.get_columns(self.columns, add_flags=False)

    @property
    def coords(self):
        if self._coords is None:
            self._get_coords()
        return self._coords

    @property
    def ra(self):
        return self.coords['ra']

    @property
    def dec(self):
        return self.coords['dec']

    @property
    def index(self):
        return self.coords.index

class MatchedCatalog(Catalog):
    def __init__(self, cat1, cat2, match_radius=0.5, tags=None, client=None):
        self.cat1 = cat1
        self.cat2 = cat2

        self.tags = ['1', '2'] if tags is None else tags

        self.match_radius = match_radius

        self.client = client

        self._coords = None

        self._match_distance = None
        self._match_inds1 = None
        self._match_inds2 = None
        self._bad_inds = None

    @property
    def coords(self):
        return self.cat1.coords

    def match(self):
        return self._match_cats()

    def _match_cats(self):
        ra1, dec1 = self.cat1.ra, self.cat1.dec
        ra2, dec2 = self.cat2.ra, self.cat2.dec
        id1 = ra1.index
        id2 = ra2.index

        dist, inds = match_lists(ra1, dec1, ra2, dec2, self.match_radius/3600)

        good = np.isfinite(dist)

        print('{0} good matches, {1} bad.'.format(good.sum(), (~good).sum()))

        # Save indices as labels, not positions, as required by dask
        self._match_inds1 = id1[good]
        self._match_inds2 = id2[inds[good]]
        self._match_distance = pd.Series(dist[good] * 3600, index=id1[good])
        self._bad_inds = id1[~good]

    @property
    def match_distance(self):
        if self._match_distance is None:
            self._match_cats()
        return self._match_distance

    @property
    def match_inds1(self):
        if self._match_inds1 is None:
            self._match_cats()
        return self._match_inds1

    @property
    def match_inds2(self):
        if self._match_inds2 is None:
            self._match_cats()
        return self._match_inds2

    @property
    def match_inds(self):
        return self.match_inds1, self.match_inds2

    def get_columns(self, *args, **kwargs):

        df1 = self.cat1.get_columns(*args, **kwargs)
        df2 = self.cat2.get_columns(*args, **kwargs)
        # df2.set_index(dd.Series(df1.index))

        return df1, df2

class MultiMatchedCatalog(MatchedCatalog):
    def __init__(self, coadd_cat, visit_cats, match_radius=0.5, client=None):
        self.coadd_cat = coadd_cat
        # Test each visit cat
        good_visit_cats = []
        for v in visit_cats:
            try:
                v.columns
                good_visit_cats.append(v)
            except:
                continue

        self.visit_cats = good_visit_cats
        self.match_radius = match_radius
        self.client = client

        self.subcats = [MatchedCatalog(self.coadd_cat, v) for v in self.visit_cats]

    @property
    def cat1(self):
        return self.coadd_cat

    def match(self):
        for i,c in enumerate(self.subcats):
            try:
                c.match()
            except:
                logging.warning('Skipping catalog {}.'.format(i))

    def get_columns(self, *args, **kwargs):
        """Returns list of dataframes: df1, then N x other dfs
        """
        df1 = self.coadd_cat.get_columns(*args, **kwargs)
        return df1, tuple(c.get_columns(*args, **kwargs) for c in self.visit_cats)

    @property
    def match_inds(self):
        ind2s = []
        for c in self.subcats:
            ind1, ind2 = c.match_inds
            ind2s.append(ind2)
        return tuple([ind1] + ind2s)

class ParquetCatalog(Catalog):
    def __init__(self, filenames, client=None):
        if type(filenames) not in [list, tuple]:
            self.filenames = [filenames]
        self.filenames = filenames
        self.client = client
        self._coords = None

        self._df = None
        self._columns = None
        self._flags = None


    @property
    def columns(self):
        if self._columns is None:
            self._columns = list(dd.read_parquet(self.filenames[0]).columns)
        return self._columns

    @property
    def flags(self):
        if self._flags is None:
            self._flags = list(dd.read_parquet(self.filenames[0]).select_dtypes(include=['bool']).columns)
        return self._flags

    def get_flags(self, flags=None):
        flags = self.flags if flags is None else flags

        return self.get_columns(flags)

    def _read_data(self, columns, query=None, add_flags=True):
        if add_flags:
            columns = columns + self.flags
        if self.client:
            df = self.client.persist(dd.read_parquet(self.filenames, columns=columns))
        else:
            df = dd.read_parquet(self.filenames, columns=columns)

        if query:
            df = df.query(query)

        if 'dir0' in df.columns:
            df = df.drop('dir0', axis=1)

        return df

    @property
    def df(self):
        if isinstance(self._df, Future):
            return self._df.result()
        else:
            return self._df

    def get_columns(self, columns, query=None, use_cache=False, add_flags=False):
        
        if use_cache and False:
            if self._df is None:
                cols_to_get = list(columns)

                if self.client:
                    self._df = self.client.persist(self._read_data(cols_to_get))
                else:
                    self._df = self._read_data(cols_to_get)

            else:
                cols_to_get = list(set(columns) - set(self._df.columns))
                if cols_to_get:
                    new = self._read_data(cols_to_get)
                    if self.client:
                        self._df = self.client.persist(self._df.merge(new))
                    else:
                        self._df = self._df.merge(new)

            if self.client:
                return self.client.persist(self.df[list(columns)])
            else:
                return self.df[list(columns)]

        else:
            cols_to_get = list(columns)
            return self._read_data(cols_to_get, query=query, add_flags=add_flags)
