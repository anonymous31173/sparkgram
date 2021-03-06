"""
Utility functions for Sparkgram
========================
"""
from scipy.sparse import csr_matrix
import numpy as np, scipy
from functools import partial


def make_csr_matrix_index(max_index) : 
    return partial(make_csr_matrix, max_index = max_index)

def make_csr_matrix(features, max_index) : 

    indices = np.array([p[0] for p in features], dtype = np.int64)
    values  = np.array([p[1] for p in features], dtype = np.float64)
    
    assert(np.alltrue(indices >= 0))
    assert(np.alltrue(values >= 0))
    
    if len(indices) > 0 : 
        return csr_matrix((values, (np.zeros(len(indices)), indices)), shape=(1,max_index+1))

class ColumnStats(object) : 
    """Column statistics for python RDDs with sparse matrices"""

    def __init__(self, rdd) :
        self._rdd = rdd
        self._mean = None
        self._norm = None
        self._var = None
        self._N = None

    @property
    def N(self):
        rdd = self._rdd
        if self._N is None : 
            self._N = rdd.count()
        return self._N

    @property
    def mean(self) :
        rdd = self._rdd
        N = self.N
        if self._mean is None: 
            res = rdd.reduce(np.add)
            self._mean = res
        return reshape_csr_to_array(self._mean)/N

    @property
    def norm(self) :
        rdd = self._rdd
        if self._norm is None:
            res = rdd.map(square_csr).reduce(np.add)
            res.data = np.sqrt(res.data)
            res = reshape_csr_to_array(res)
            self._norm = res
        return self._norm

    @property
    def var(self) : 
        rdd = self._rdd
        if self._var is None : 
            mean = self.mean
            N = self._N
            self._var = rdd.map(lambda vec: np.frombuffer(vec - mean)**2).reduce(np.add)/N
        return reshape_csr_to_array(self._var)
    
    @property
    def std(self) : 
        return np.sqrt(self.var)
            
def online_variance(stats, vec_iter) :
    n, nnz, mean, M2 = stats['n'], stats['nnz'], stats['mean'], stats['M2']
    
    for vec in vec_iter :  
        if type(vec) ==  csr_matrix : 
            indices = vec.sorted_indices().indices
            data = vec.sorted_indices().data        
        elif type(vec) == np.ndarray: 
            indices = vec.nonzero()
            data = vec[indices]
        else : 
            raise RuntimeError('Data must be either scipy.sparse.csr_matrix or numpy.ndarray, but got %s instead'%(type(vec)))

        n += 1
        nnz[indices] += 1
        delta = data - mean[indices]
        mean[indices] += delta/nnz[indices]
        M2[indices] += delta*(data - mean[indices])
        
    return ColumnStatDict({'n':n, 'nnz':nnz, 'mean':mean, 'M2':M2})

def online_variance_agg(res1, res2) : 
    res1['n'] += res2['n']
    mean = res1['mean']
    M2 = res1['M2']
    
    other_nnz = res2['nnz'].nonzero() # use only non-zero elements to avoid division by zero
    delta = res2['mean'] - res1['mean']

    tot_nnz = res1['nnz'][other_nnz] + res2['nnz'][other_nnz]

    mean[other_nnz] += delta[other_nnz] * res2['nnz'][other_nnz] / tot_nnz

    M2[other_nnz] += res2['M2'][other_nnz] + delta[other_nnz]**2 * res1['nnz'][other_nnz] * res2['nnz'][other_nnz] / tot_nnz

    res1['nnz'][other_nnz] = tot_nnz
    
    return res1
    
class ColumnStatDict(object) : 
    def __init__(self, valsdict = None, size = None, sample = False):
        if valsdict is None : 
            if size is None : raise RuntimeError('Size must be set')
            valsdict = {'n':0,'nnz':np.zeros(size),'mean':np.zeros(size),'M2':np.zeros(size)}
        self._myvals = valsdict
        self._sample = sample

    def __getitem__(self, key) : 
        return self._myvals[key]
    
    def __setitem__(self, key, value):
        self._myvals[key] = value

    @property
    def std(self):
        # need to scale M2 to account for the zeros that change the means
        vals = self._myvals
        n, nnz, mean, M2 = [vals[key] for key in ['n','nnz','mean','M2']]
#        M2_final = M2 + mean**2 * nnz**2 / (n**2 - n) # this should be equivalent to the line below but it isnt...
        M2_final = M2 + mean * mean * nnz * (n - nnz) / n # this is taken from the spark implementation
        if self._sample : 
            return np.sqrt(M2_final/(n-1))
        else : 
            return np.sqrt(M2_final/(n))
        

    @property
    def mean(self):
        return self['mean'] * self['nnz'] / self['n']

    def transform(self, vec) :
        std = self.std
        std[std == 0.0] = 1.0
        return vec / std


def reshape_csr_to_array(csr) : 
    if csr.shape[1]*8 < (csr.data.nbytes + csr.indptr.nbytes + csr.indices.nbytes) : 
        csr = csr.toarray().squeeze()
    return csr 

def calculate_column_stat(rdd, op = 'mean') : 
    """Given an RDD of sparse feature vectors, calculate various quantities
    `rdd` should be an rdd of `scipy.sparse.csr_matrix` 

    `op` can be 'mean' or 'norm' 
    """ 
    n_items = rdd.count()

    if op == 'mean':
        res = rdd.reduce(np.add)
    if op == 'norm': 
        res = rdd.map(square_csr).reduce(np.add)
        res.data = np.sqrt(res.data)

    if res.shape[1]*8 < (res.data.nbytes + res.indptr.nbytes + res.indices.nbytes) : 
        res = res.toarray().squeeze()

    return res

def square_csr(vec) : 
    """Square the individual elements of a csr matrix"""
    vec.data *= vec.data
    return vec


def add_arrays(arr1, arr2) : 
    """Add the two sparse arrays but return a dense array if it saves memory"""
    res = arr1 + arr2
    if type(res) is scipy.sparse.csr.csr_matrix: 
        if res.shape[1]*8 < (res.data.nbytes + res.indptr.nbytes + res.indices.nbytes) : 
            return res.toarray()
    return res


from bisect import bisect_left

class TopNgramsAggregator(object) :
    
    def __init__(self, N, filt = None) :
        self.N = N
        self.filt = filt
        self.result = []
        self.min = 0
        self.max = 1e99
        
    def add_new_value(self, val) : 
        ngram, count = val
        new_val = (count, ngram)
        
        if count >= self.min : 
            new_pos = bisect_left(map(lambda x: x[0], self.result), count)
            self.result.insert(new_pos, new_val)
            if len(self.result) > self.N :
                self.result = self.result[-self.N:]
            self.min = self.result[0][0]
        return self
    
    def merge_other_result(self, other_result) : 
        self.result.extend(other_result.result)
        new_res = set(self.result)
        new_res = list(new_res)
        new_res.sort(reverse=True)
        self.result = new_res[:self.N]
        return self
