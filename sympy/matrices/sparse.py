from collections import defaultdict
from collections.abc import Callable
from functools import reduce

from sympy.core import SympifyError, Add
from sympy.core.compatibility import as_int, is_sequence
from sympy.core.containers import Dict
from sympy.core.expr import Expr
from sympy.core.singleton import S
from sympy.core.sympify import _sympify
from sympy.functions import Abs
from sympy.utilities.iterables import uniq

from .common import a2idx
from .dense import Matrix
from .matrices import MatrixBase, ShapeError

from .utilities import _iszero

from .decompositions import (
    _liupc, _row_structure_symbolic_cholesky, _cholesky_sparse,
    _LDLdecomposition_sparse)

from .solvers import (
    _lower_triangular_solve_sparse, _upper_triangular_solve_sparse)


class SparseMatrix(MatrixBase):
    """
    A sparse matrix (a matrix with a large number of zero elements).

    Examples
    ========

    >>> from sympy.matrices import SparseMatrix, ones
    >>> SparseMatrix(2, 2, range(4))
    Matrix([
    [0, 1],
    [2, 3]])
    >>> SparseMatrix(2, 2, {(1, 1): 2})
    Matrix([
    [0, 0],
    [0, 2]])

    A SparseMatrix can be instantiated from a ragged list of lists:

    >>> SparseMatrix([[1, 2, 3], [1, 2], [1]])
    Matrix([
    [1, 2, 3],
    [1, 2, 0],
    [1, 0, 0]])

    For safety, one may include the expected size and then an error
    will be raised if the indices of any element are out of range or
    (for a flat list) if the total number of elements does not match
    the expected shape:

    >>> SparseMatrix(2, 2, [1, 2])
    Traceback (most recent call last):
    ...
    ValueError: List length (2) != rows*columns (4)

    Here, an error is not raised because the list is not flat and no
    element is out of range:

    >>> SparseMatrix(2, 2, [[1, 2]])
    Matrix([
    [1, 2],
    [0, 0]])

    But adding another element to the first (and only) row will cause
    an error to be raised:

    >>> SparseMatrix(2, 2, [[1, 2, 3]])
    Traceback (most recent call last):
    ...
    ValueError: The location (0, 2) is out of designated range: (1, 1)

    To autosize the matrix, pass None for rows:

    >>> SparseMatrix(None, [[1, 2, 3]])
    Matrix([[1, 2, 3]])
    >>> SparseMatrix(None, {(1, 1): 1, (3, 3): 3})
    Matrix([
    [0, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 0, 0, 0],
    [0, 0, 0, 3]])

    Values that are themselves a Matrix are automatically expanded:

    >>> SparseMatrix(4, 4, {(1, 1): ones(2)})
    Matrix([
    [0, 0, 0, 0],
    [0, 1, 1, 0],
    [0, 1, 1, 0],
    [0, 0, 0, 0]])

    A ValueError is raised if the expanding matrix tries to overwrite
    a different element already present:

    >>> SparseMatrix(3, 3, {(0, 0): ones(2), (1, 1): 2})
    Traceback (most recent call last):
    ...
    ValueError: collision at (1, 1)

    See Also
    ========
    DenseMatrix
    MutableSparseMatrix
    ImmutableSparseMatrix
    """

    @classmethod
    def _handle_creation_inputs(cls, *args, **kwargs):
        if len(args) == 1 and isinstance(args[0], MatrixBase):
            rows = args[0].rows
            cols = args[0].cols
            smat = args[0].todok()
            return rows, cols, smat

        smat = {}
        # autosizing
        if len(args) == 2 and args[0] is None:
            args = [None, None, args[1]]

        if len(args) == 3:
            r, c = args[:2]
            if r is c is None:
                rows = cols = None
            elif None in (r, c):
                raise ValueError(
                    'Pass rows=None and no cols for autosizing.')
            else:
                rows, cols = as_int(args[0]), as_int(args[1])

            if isinstance(args[2], Callable):
                op = args[2]

                if None in (rows, cols):
                    raise ValueError(
                        "{} and {} must be integers for this "
                        "specification.".format(rows, cols))

                row_indices = [cls._sympify(i) for i in range(rows)]
                col_indices = [cls._sympify(j) for j in range(cols)]

                for i in row_indices:
                    for j in col_indices:
                        value = cls._sympify(op(i, j))
                        if value != cls.zero:
                            smat[i, j] = value

                return rows, cols, smat

            elif isinstance(args[2], (dict, Dict)):
                def update(i, j, v):
                    # update self._smat and make sure there are
                    # no collisions
                    if v:
                        if (i, j) in smat and v != smat[i, j]:
                            raise ValueError(
                                "There is a collision at {} for {} and {}."
                                .format((i, j), v, smat[i, j])
                            )
                        smat[i, j] = v

                # manual copy, copy.deepcopy() doesn't work
                for (r, c), v in args[2].items():
                    if isinstance(v, MatrixBase):
                        for (i, j), vv in v.todok().items():
                            update(r + i, c + j, vv)
                    elif isinstance(v, (list, tuple)):
                        _, _, smat = cls._handle_creation_inputs(v, **kwargs)
                        for i, j in smat:
                            update(r + i, c + j, smat[i, j])
                    else:
                        v = cls._sympify(v)
                        update(r, c, cls._sympify(v))

            elif is_sequence(args[2]):
                flat = not any(is_sequence(i) for i in args[2])
                if not flat:
                    _, _, smat = \
                        cls._handle_creation_inputs(args[2], **kwargs)
                else:
                    flat_list = args[2]
                    if len(flat_list) != rows * cols:
                        raise ValueError(
                            "The length of the flat list ({}) does not "
                            "match the specified size ({} * {})."
                            .format(len(flat_list), rows, cols)
                        )

                    for i in range(rows):
                        for j in range(cols):
                            value = flat_list[i*cols + j]
                            value = cls._sympify(value)
                            if value != cls.zero:
                                smat[i, j] = value

            if rows is None:  # autosizing
                keys = smat.keys()
                rows = max([r for r, _ in keys]) + 1 if keys else 0
                cols = max([c for _, c in keys]) + 1 if keys else 0

            else:
                for i, j in smat.keys():
                    if i and i >= rows or j and j >= cols:
                        raise ValueError(
                            "The location {} is out of the designated range"
                            "[{}, {}]x[{}, {}]"
                            .format((i, j), 0, rows - 1, 0, cols - 1)
                        )

            return rows, cols, smat

        elif len(args) == 1 and isinstance(args[0], (list, tuple)):
            # list of values or lists
            v = args[0]
            c = 0
            for i, row in enumerate(v):
                if not isinstance(row, (list, tuple)):
                    row = [row]
                for j, vv in enumerate(row):
                    if vv != cls.zero:
                        smat[i, j] = cls._sympify(vv)
                c = max(c, len(row))
            rows = len(v) if c else 0
            cols = c
            return rows, cols, smat

        else:
            # handle full matrix forms with _handle_creation_inputs
            rows, cols, mat = super()._handle_creation_inputs(*args)
            for i in range(rows):
                for j in range(cols):
                    value = mat[cols*i + j]
                    if value != cls.zero:
                        smat[i, j] = value

            return rows, cols, smat

    def __eq__(self, other):
        try:
            other = _sympify(other)
        except SympifyError:
            return NotImplemented
        self_shape = getattr(self, 'shape', None)
        other_shape = getattr(other, 'shape', None)
        if None in (self_shape, other_shape):
            return False
        if self_shape != other_shape:
            return False
        if isinstance(other, SparseMatrix):
            return self._smat == other._smat
        elif isinstance(other, MatrixBase):
            return self._smat == MutableSparseMatrix(other)._smat

    def __getitem__(self, key):

        if isinstance(key, tuple):
            i, j = key
            try:
                i, j = self.key2ij(key)
                return self._smat.get((i, j), S.Zero)
            except (TypeError, IndexError):
                if isinstance(i, slice):
                    i = range(self.rows)[i]
                elif is_sequence(i):
                    pass
                elif isinstance(i, Expr) and not i.is_number:
                    from sympy.matrices.expressions.matexpr import MatrixElement
                    return MatrixElement(self, i, j)
                else:
                    if i >= self.rows:
                        raise IndexError('Row index out of bounds')
                    i = [i]
                if isinstance(j, slice):
                    j = range(self.cols)[j]
                elif is_sequence(j):
                    pass
                elif isinstance(j, Expr) and not j.is_number:
                    from sympy.matrices.expressions.matexpr import MatrixElement
                    return MatrixElement(self, i, j)
                else:
                    if j >= self.cols:
                        raise IndexError('Col index out of bounds')
                    j = [j]
                return self.extract(i, j)

        # check for single arg, like M[:] or M[3]
        if isinstance(key, slice):
            lo, hi = key.indices(len(self))[:2]
            L = []
            for i in range(lo, hi):
                m, n = divmod(i, self.cols)
                L.append(self._smat.get((m, n), S.Zero))
            return L

        i, j = divmod(a2idx(key, len(self)), self.cols)
        return self._smat.get((i, j), S.Zero)

    def __setitem__(self, key, value):
        raise NotImplementedError()

    def _eval_inverse(self, **kwargs):
        return self.inv(method=kwargs.get('method', 'LDL'),
                        iszerofunc=kwargs.get('iszerofunc', _iszero),
                        try_block_diag=kwargs.get('try_block_diag', False))

    def _eval_Abs(self):
        return self.applyfunc(lambda x: Abs(x))

    def _eval_add(self, other):
        """If `other` is a SparseMatrix, add efficiently. Otherwise,
        do standard addition."""
        if not isinstance(other, SparseMatrix):
            return self + self._new(other)

        smat = {}
        zero = self._sympify(0)
        for key in set().union(self._smat.keys(), other._smat.keys()):
            sum = self._smat.get(key, zero) + other._smat.get(key, zero)
            if sum != 0:
                smat[key] = sum
        return self._new(self.rows, self.cols, smat)

    def _eval_col_insert(self, icol, other):
        if not isinstance(other, SparseMatrix):
            other = MutableSparseMatrix(other)
        new_smat = {}
        # make room for the new rows
        for key, val in self._smat.items():
            row, col = key
            if col >= icol:
                col += other.cols
            new_smat[row, col] = val
        # add other's keys
        for key, val in other._smat.items():
            row, col = key
            new_smat[row, col + icol] = val
        return self._new(self.rows, self.cols + other.cols, new_smat)

    def _eval_conjugate(self):
        smat = {key: val.conjugate() for key,val in self._smat.items()}
        return self._new(self.rows, self.cols, smat)

    def _eval_extract(self, rowsList, colsList):
        urow = list(uniq(rowsList))
        ucol = list(uniq(colsList))
        smat = {}
        if len(urow)*len(ucol) < len(self._smat):
            # there are fewer elements requested than there are elements in the matrix
            for i, r in enumerate(urow):
                for j, c in enumerate(ucol):
                    smat[i, j] = self._smat.get((r, c), 0)
        else:
            # most of the request will be zeros so check all of self's entries,
            # keeping only the ones that are desired
            for rk, ck in self._smat:
                if rk in urow and ck in ucol:
                    smat[urow.index(rk), ucol.index(ck)] = self._smat[rk, ck]

        rv = self._new(len(urow), len(ucol), smat)
        # rv is nominally correct but there might be rows/cols
        # which require duplication
        if len(rowsList) != len(urow):
            for i, r in enumerate(rowsList):
                i_previous = rowsList.index(r)
                if i_previous != i:
                    rv = rv.row_insert(i, rv.row(i_previous))
        if len(colsList) != len(ucol):
            for i, c in enumerate(colsList):
                i_previous = colsList.index(c)
                if i_previous != i:
                    rv = rv.col_insert(i, rv.col(i_previous))
        return rv

    @classmethod
    def _eval_eye(cls, rows, cols):
        entries = {(i,i): S.One for i in range(min(rows, cols))}
        return cls._new(rows, cols, entries)

    def _eval_has(self, *patterns):
        # if the matrix has any zeros, see if S.Zero
        # has the pattern.  If _smat is full length,
        # the matrix has no zeros.
        zhas = S.Zero.has(*patterns)
        if len(self._smat) == self.rows*self.cols:
            zhas = False
        return any(self[key].has(*patterns) for key in self._smat) or zhas

    def _eval_is_Identity(self):
        if not all(self[i, i] == 1 for i in range(self.rows)):
            return False
        return len(self._smat) == self.rows

    def _eval_is_symmetric(self, simpfunc):
        diff = (self - self.T).applyfunc(simpfunc)
        return len(diff.values()) == 0

    def _eval_matrix_mul(self, other):
        """Fast multiplication exploiting the sparsity of the matrix."""

        if not isinstance(other, SparseMatrix):
            other = self._new(other)

        # if we made it here, we're both sparse matrices
        # create quick lookups for rows and cols
        row_lookup = defaultdict(dict)
        for (i,j), val in self._smat.items():
            row_lookup[i][j] = val
        col_lookup = defaultdict(dict)
        for (i,j), val in other._smat.items():
            col_lookup[j][i] = val

        smat = {}
        for row in row_lookup.keys():
            for col in col_lookup.keys():
                # find the common indices of non-zero entries.
                # these are the only things that need to be multiplied.
                indices = set(col_lookup[col].keys()) & set(row_lookup[row].keys())
                if indices:
                    vec = [row_lookup[row][k]*col_lookup[col][k] for k in indices]
                    try:
                        smat[row, col] = Add(*vec)
                    except (TypeError, SympifyError):
                        # Some matrices don't work with `sum` or `Add`
                        # They don't work with `sum` because `sum` tries to add `0`
                        # Fall back to a safe way to multiply if the `Add` fails.
                        smat[row, col] = reduce(lambda a, b: a + b, vec)

        return self._new(self.rows, other.cols, smat)

    def _eval_row_insert(self, irow, other):
        if not isinstance(other, SparseMatrix):
            other = MutableSparseMatrix(other)
        new_smat = {}
        # make room for the new rows
        for key, val in self._smat.items():
            row, col = key
            if row >= irow:
                row += other.rows
            new_smat[row, col] = val
        # add other's keys
        for key, val in other._smat.items():
            row, col = key
            new_smat[row + irow, col] = val
        return self._new(self.rows + other.rows, self.cols, new_smat)

    def _eval_scalar_mul(self, other):
        return self.applyfunc(lambda x: x*other)

    def _eval_scalar_rmul(self, other):
        return self.applyfunc(lambda x: other*x)

    def _eval_todok(self):
        return self._smat.copy()

    def _eval_transpose(self):
        """Returns the transposed SparseMatrix of this SparseMatrix.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> a = SparseMatrix(((1, 2), (3, 4)))
        >>> a
        Matrix([
        [1, 2],
        [3, 4]])
        >>> a.T
        Matrix([
        [1, 3],
        [2, 4]])
        """
        smat = {(j,i): val for (i,j),val in self._smat.items()}
        return self._new(self.cols, self.rows, smat)

    def _eval_values(self):
        return [v for k,v in self._smat.items() if not v.is_zero]

    @classmethod
    def _eval_zeros(cls, rows, cols):
        return cls._new(rows, cols, {})

    @property
    def _mat(self):
        """Return a list of matrix elements.  Some routines
        in DenseMatrix use `_mat` directly to speed up operations."""
        return list(self)

    def applyfunc(self, f):
        """Apply a function to each element of the matrix.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> m = SparseMatrix(2, 2, lambda i, j: i*2+j)
        >>> m
        Matrix([
        [0, 1],
        [2, 3]])
        >>> m.applyfunc(lambda i: 2*i)
        Matrix([
        [0, 2],
        [4, 6]])

        """
        if not callable(f):
            raise TypeError("`f` must be callable.")

        out = self.copy()
        for k, v in self._smat.items():
            fv = f(v)
            if fv:
                out._smat[k] = fv
            else:
                out._smat.pop(k, None)
        return out

    def as_immutable(self):
        """Returns an Immutable version of this Matrix."""
        from .immutable import ImmutableSparseMatrix
        return ImmutableSparseMatrix(self)

    def as_mutable(self):
        """Returns a mutable version of this matrix.

        Examples
        ========

        >>> from sympy import ImmutableMatrix
        >>> X = ImmutableMatrix([[1, 2], [3, 4]])
        >>> Y = X.as_mutable()
        >>> Y[1, 1] = 5 # Can set values in Y
        >>> Y
        Matrix([
        [1, 2],
        [3, 5]])
        """
        return MutableSparseMatrix(self)

    def col_list(self):
        """Returns a column-sorted list of non-zero elements of the matrix.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> a=SparseMatrix(((1, 2), (3, 4)))
        >>> a
        Matrix([
        [1, 2],
        [3, 4]])
        >>> a.CL
        [(0, 0, 1), (1, 0, 3), (0, 1, 2), (1, 1, 4)]

        See Also
        ========
        sympy.matrices.sparse.MutableSparseMatrix.col_op
        sympy.matrices.sparse.SparseMatrix.row_list
        """
        return [tuple(k + (self[k],)) for k in sorted(list(self._smat.keys()), key=lambda k: list(reversed(k)))]

    def copy(self):
        return self._new(self.rows, self.cols, self._smat)

    def nnz(self):
        """Returns the number of non-zero elements in Matrix."""
        return len(self._smat)

    def row_list(self):
        """Returns a row-sorted list of non-zero elements of the matrix.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> a = SparseMatrix(((1, 2), (3, 4)))
        >>> a
        Matrix([
        [1, 2],
        [3, 4]])
        >>> a.RL
        [(0, 0, 1), (0, 1, 2), (1, 0, 3), (1, 1, 4)]

        See Also
        ========
        sympy.matrices.sparse.MutableSparseMatrix.row_op
        sympy.matrices.sparse.SparseMatrix.col_list
        """
        return [tuple(k + (self[k],)) for k in
            sorted(list(self._smat.keys()), key=lambda k: list(k))]

    def scalar_multiply(self, scalar):
        "Scalar element-wise multiplication"
        M = self.zeros(*self.shape)
        if scalar:
            for i in self._smat:
                v = scalar*self._smat[i]
                if v:
                    M._smat[i] = v
                else:
                    M._smat.pop(i, None)
        return M

    def solve_least_squares(self, rhs, method='LDL'):
        """Return the least-square fit to the data.

        By default the cholesky_solve routine is used (method='CH'); other
        methods of matrix inversion can be used. To find out which are
        available, see the docstring of the .inv() method.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix, Matrix, ones
        >>> A = Matrix([1, 2, 3])
        >>> B = Matrix([2, 3, 4])
        >>> S = SparseMatrix(A.row_join(B))
        >>> S
        Matrix([
        [1, 2],
        [2, 3],
        [3, 4]])

        If each line of S represent coefficients of Ax + By
        and x and y are [2, 3] then S*xy is:

        >>> r = S*Matrix([2, 3]); r
        Matrix([
        [ 8],
        [13],
        [18]])

        But let's add 1 to the middle value and then solve for the
        least-squares value of xy:

        >>> xy = S.solve_least_squares(Matrix([8, 14, 18])); xy
        Matrix([
        [ 5/3],
        [10/3]])

        The error is given by S*xy - r:

        >>> S*xy - r
        Matrix([
        [1/3],
        [1/3],
        [1/3]])
        >>> _.norm().n(2)
        0.58

        If a different xy is used, the norm will be higher:

        >>> xy += ones(2, 1)/10
        >>> (S*xy - r).norm().n(2)
        1.5

        """
        t = self.T
        return (t*self).inv(method=method)*t*rhs

    def solve(self, rhs, method='LDL'):
        """Return solution to self*soln = rhs using given inversion method.

        For a list of possible inversion methods, see the .inv() docstring.
        """
        if not self.is_square:
            if self.rows < self.cols:
                raise ValueError('Under-determined system.')
            elif self.rows > self.cols:
                raise ValueError('For over-determined system, M, having '
                    'more rows than columns, try M.solve_least_squares(rhs).')
        else:
            return self.inv(method=method).multiply(rhs)

    RL = property(row_list, None, None, "Alternate faster representation")
    CL = property(col_list, None, None, "Alternate faster representation")

    def liupc(self):
        return _liupc(self)

    def row_structure_symbolic_cholesky(self):
        return _row_structure_symbolic_cholesky(self)

    def cholesky(self, hermitian=True):
        return _cholesky_sparse(self, hermitian=hermitian)

    def LDLdecomposition(self, hermitian=True):
        return _LDLdecomposition_sparse(self, hermitian=hermitian)

    def lower_triangular_solve(self, rhs):
        return _lower_triangular_solve_sparse(self, rhs)

    def upper_triangular_solve(self, rhs):
        return _upper_triangular_solve_sparse(self, rhs)

    liupc.__doc__                           = _liupc.__doc__
    row_structure_symbolic_cholesky.__doc__ = _row_structure_symbolic_cholesky.__doc__
    cholesky.__doc__                        = _cholesky_sparse.__doc__
    LDLdecomposition.__doc__                = _LDLdecomposition_sparse.__doc__
    lower_triangular_solve.__doc__          = lower_triangular_solve.__doc__
    upper_triangular_solve.__doc__          = upper_triangular_solve.__doc__


class MutableSparseMatrix(SparseMatrix, MatrixBase):
    def __new__(cls, *args, **kwargs):
        return cls._new(*args, **kwargs)

    @classmethod
    def _new(cls, *args, **kwargs):
        obj = super().__new__(cls)
        rows, cols, smat = cls._handle_creation_inputs(*args, **kwargs)
        obj.rows = rows
        obj.cols = cols
        obj._smat = smat
        return obj

    def __setitem__(self, key, value):
        """Assign value to position designated by key.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix, ones
        >>> M = SparseMatrix(2, 2, {})
        >>> M[1] = 1; M
        Matrix([
        [0, 1],
        [0, 0]])
        >>> M[1, 1] = 2; M
        Matrix([
        [0, 1],
        [0, 2]])
        >>> M = SparseMatrix(2, 2, {})
        >>> M[:, 1] = [1, 1]; M
        Matrix([
        [0, 1],
        [0, 1]])
        >>> M = SparseMatrix(2, 2, {})
        >>> M[1, :] = [[1, 1]]; M
        Matrix([
        [0, 0],
        [1, 1]])


        To replace row r you assign to position r*m where m
        is the number of columns:

        >>> M = SparseMatrix(4, 4, {})
        >>> m = M.cols
        >>> M[3*m] = ones(1, m)*2; M
        Matrix([
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [2, 2, 2, 2]])

        And to replace column c you can assign to position c:

        >>> M[2] = ones(m, 1)*4; M
        Matrix([
        [0, 0, 4, 0],
        [0, 0, 4, 0],
        [0, 0, 4, 0],
        [2, 2, 4, 2]])
        """
        rv = self._setitem(key, value)
        if rv is not None:
            i, j, value = rv
            if value:
                self._smat[i, j] = value
            elif (i, j) in self._smat:
                del self._smat[i, j]

    def as_mutable(self):
        return self.copy()

    __hash__ = None  # type: ignore

    def _eval_col_del(self, k):
        newD = {}
        for i, j in self._smat:
            if j == k:
                pass
            elif j > k:
                newD[i, j - 1] = self._smat[i, j]
            else:
                newD[i, j] = self._smat[i, j]
        self._smat = newD
        self.cols -= 1

    def _eval_row_del(self, k):
        newD = {}
        for i, j in self._smat:
            if i == k:
                pass
            elif i > k:
                newD[i - 1, j] = self._smat[i, j]
            else:
                newD[i, j] = self._smat[i, j]
        self._smat = newD
        self.rows -= 1

    def col_join(self, other):
        """Returns B augmented beneath A (row-wise joining)::

            [A]
            [B]

        Examples
        ========

        >>> from sympy import SparseMatrix, Matrix, ones
        >>> A = SparseMatrix(ones(3))
        >>> A
        Matrix([
        [1, 1, 1],
        [1, 1, 1],
        [1, 1, 1]])
        >>> B = SparseMatrix.eye(3)
        >>> B
        Matrix([
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1]])
        >>> C = A.col_join(B); C
        Matrix([
        [1, 1, 1],
        [1, 1, 1],
        [1, 1, 1],
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1]])
        >>> C == A.col_join(Matrix(B))
        True

        Joining along columns is the same as appending rows at the end
        of the matrix:

        >>> C == A.row_insert(A.rows, Matrix(B))
        True
        """
        # A null matrix can always be stacked (see  #10770)
        if self.rows == 0 and self.cols != other.cols:
            return self._new(0, other.cols, []).col_join(other)

        A, B = self, other
        if not A.cols == B.cols:
            raise ShapeError()
        A = A.copy()
        if not isinstance(B, SparseMatrix):
            k = 0
            b = B._mat
            for i in range(B.rows):
                for j in range(B.cols):
                    v = b[k]
                    if v:
                        A._smat[i + A.rows, j] = v
                    k += 1
        else:
            for (i, j), v in B._smat.items():
                A._smat[i + A.rows, j] = v
        A.rows += B.rows
        return A

    def col_op(self, j, f):
        """In-place operation on col j using two-arg functor whose args are
        interpreted as (self[i, j], i) for i in range(self.rows).

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> M = SparseMatrix.eye(3)*2
        >>> M[1, 0] = -1
        >>> M.col_op(1, lambda v, i: v + 2*M[i, 0]); M
        Matrix([
        [ 2, 4, 0],
        [-1, 0, 0],
        [ 0, 0, 2]])
        """
        for i in range(self.rows):
            v = self._smat.get((i, j), S.Zero)
            fv = f(v, i)
            if fv:
                self._smat[i, j] = fv
            elif v:
                self._smat.pop((i, j))

    def col_swap(self, i, j):
        """Swap, in place, columns i and j.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> S = SparseMatrix.eye(3); S[2, 1] = 2
        >>> S.col_swap(1, 0); S
        Matrix([
        [0, 1, 0],
        [1, 0, 0],
        [2, 0, 1]])
        """
        if i > j:
            i, j = j, i
        rows = self.col_list()
        temp = []
        for ii, jj, v in rows:
            if jj == i:
                self._smat.pop((ii, jj))
                temp.append((ii, v))
            elif jj == j:
                self._smat.pop((ii, jj))
                self._smat[ii, i] = v
            elif jj > j:
                break
        for k, v in temp:
            self._smat[k, j] = v

    def copyin_list(self, key, value):
        if not is_sequence(value):
            raise TypeError("`value` must be of type list or tuple.")
        self.copyin_matrix(key, Matrix(value))

    def copyin_matrix(self, key, value):
        # include this here because it's not part of BaseMatrix
        rlo, rhi, clo, chi = self.key2bounds(key)
        shape = value.shape
        dr, dc = rhi - rlo, chi - clo
        if shape != (dr, dc):
            raise ShapeError(
                "The Matrix `value` doesn't have the same dimensions "
                "as the in sub-Matrix given by `key`.")
        if not isinstance(value, SparseMatrix):
            for i in range(value.rows):
                for j in range(value.cols):
                    self[i + rlo, j + clo] = value[i, j]
        else:
            if (rhi - rlo)*(chi - clo) < len(self):
                for i in range(rlo, rhi):
                    for j in range(clo, chi):
                        self._smat.pop((i, j), None)
            else:
                for i, j, v in self.row_list():
                    if rlo <= i < rhi and clo <= j < chi:
                        self._smat.pop((i, j), None)
            for k, v in value._smat.items():
                i, j = k
                self[i + rlo, j + clo] = value[i, j]

    def fill(self, value):
        """Fill self with the given value.

        Notes
        =====

        Unless many values are going to be deleted (i.e. set to zero)
        this will create a matrix that is slower than a dense matrix in
        operations.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> M = SparseMatrix.zeros(3); M
        Matrix([
        [0, 0, 0],
        [0, 0, 0],
        [0, 0, 0]])
        >>> M.fill(1); M
        Matrix([
        [1, 1, 1],
        [1, 1, 1],
        [1, 1, 1]])
        """
        if not value:
            self._smat = {}
        else:
            v = self._sympify(value)
            self._smat = {(i, j): v
                for i in range(self.rows) for j in range(self.cols)}

    def row_join(self, other):
        """Returns B appended after A (column-wise augmenting)::

            [A B]

        Examples
        ========

        >>> from sympy import SparseMatrix, Matrix
        >>> A = SparseMatrix(((1, 0, 1), (0, 1, 0), (1, 1, 0)))
        >>> A
        Matrix([
        [1, 0, 1],
        [0, 1, 0],
        [1, 1, 0]])
        >>> B = SparseMatrix(((1, 0, 0), (0, 1, 0), (0, 0, 1)))
        >>> B
        Matrix([
        [1, 0, 0],
        [0, 1, 0],
        [0, 0, 1]])
        >>> C = A.row_join(B); C
        Matrix([
        [1, 0, 1, 1, 0, 0],
        [0, 1, 0, 0, 1, 0],
        [1, 1, 0, 0, 0, 1]])
        >>> C == A.row_join(Matrix(B))
        True

        Joining at row ends is the same as appending columns at the end
        of the matrix:

        >>> C == A.col_insert(A.cols, B)
        True
        """
        # A null matrix can always be stacked (see  #10770)
        if self.cols == 0 and self.rows != other.rows:
            return self._new(other.rows, 0, []).row_join(other)

        A, B = self, other
        if not A.rows == B.rows:
            raise ShapeError()
        A = A.copy()
        if not isinstance(B, SparseMatrix):
            k = 0
            b = B._mat
            for i in range(B.rows):
                for j in range(B.cols):
                    v = b[k]
                    if v:
                        A._smat[i, j + A.cols] = v
                    k += 1
        else:
            for (i, j), v in B._smat.items():
                A._smat[i, j + A.cols] = v
        A.cols += B.cols
        return A

    def row_op(self, i, f):
        """In-place operation on row ``i`` using two-arg functor whose args are
        interpreted as ``(self[i, j], j)``.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> M = SparseMatrix.eye(3)*2
        >>> M[0, 1] = -1
        >>> M.row_op(1, lambda v, j: v + 2*M[0, j]); M
        Matrix([
        [2, -1, 0],
        [4,  0, 0],
        [0,  0, 2]])

        See Also
        ========
        row
        zip_row_op
        col_op

        """
        for j in range(self.cols):
            v = self._smat.get((i, j), S.Zero)
            fv = f(v, j)
            if fv:
                self._smat[i, j] = fv
            elif v:
                self._smat.pop((i, j))

    def row_swap(self, i, j):
        """Swap, in place, columns i and j.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> S = SparseMatrix.eye(3); S[2, 1] = 2
        >>> S.row_swap(1, 0); S
        Matrix([
        [0, 1, 0],
        [1, 0, 0],
        [0, 2, 1]])
        """
        if i > j:
            i, j = j, i
        rows = self.row_list()
        temp = []
        for ii, jj, v in rows:
            if ii == i:
                self._smat.pop((ii, jj))
                temp.append((jj, v))
            elif ii == j:
                self._smat.pop((ii, jj))
                self._smat[i, jj] = v
            elif ii > j:
                break
        for k, v in temp:
            self._smat[j, k] = v

    def zip_row_op(self, i, k, f):
        """In-place operation on row ``i`` using two-arg functor whose args are
        interpreted as ``(self[i, j], self[k, j])``.

        Examples
        ========

        >>> from sympy.matrices import SparseMatrix
        >>> M = SparseMatrix.eye(3)*2
        >>> M[0, 1] = -1
        >>> M.zip_row_op(1, 0, lambda v, u: v + 2*u); M
        Matrix([
        [2, -1, 0],
        [4,  0, 0],
        [0,  0, 2]])

        See Also
        ========
        row
        row_op
        col_op

        """
        self.row_op(i, lambda v, j: f(v, self[k, j]))

    is_zero = False
