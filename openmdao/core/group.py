""" Defines the base class for a Group in OpenMDAO."""

from __future__ import print_function

import sys
from collections import OrderedDict
from six import iteritems
from itertools import chain

import numpy as np

from openmdao.components.paramcomp import ParamComp
from openmdao.core.system import System
from openmdao.core.basicimpl import BasicImpl
from openmdao.core.component import Component
from openmdao.core.varmanager import VarManager, ViewVarManager, create_views
from openmdao.solvers.run_once import RunOnce
from openmdao.solvers.scipy_gmres import ScipyGMRES
from openmdao.util.types import real_types

from openmdao.core.mpiwrap import get_comm_if_active

class Group(System):
    """A system that contains other systems"""

    def __init__(self):
        super(Group, self).__init__()

        self._subsystems = OrderedDict()
        self._local_subsystems = OrderedDict()
        self._src = {}
        self._varmanager = None

        # These solvers are the default
        self.ln_solver = ScipyGMRES()
        self.nl_solver = RunOnce()

    def __setitem__(self, name, val):
        """Sets the given value into the appropriate `VecWrapper`.

        Parameters
        ----------
        name : str
             the name of the variable to set into the unknowns vector
        """
        if self.is_active():
            self._varmanager.unknowns[name] = val

    def __getitem__(self, name):
        """Retrieve unflattened value of named variable or a reference
        to named subsystem.

        Parameters
        ----------
        name : str   OR   tuple : (name, vector)
             the name of the variable to retrieve from the unknowns vector OR
             a tuple of the name of the variable and the vector to get it's
             value from.

        Returns
        -------
        the unflattened value of the given variable or a reference to the
        named subsystem.
        """

        # if arg is not a tuple, then search for a subsystem by name
        if not isinstance(name, tuple):
            sys = self
            parts = name.split(':')
            for part in parts:
                sys = getattr(sys, '_subsystems', {}).get(part)
                if sys is None:
                    break
            else:
                return sys

        # if system is not active, then it's not valid to access it's variables
        if not self.is_active():
            return None

        # if arg is a tuple or no subsystem found, then search for a variable
        if not self._varmanager:
            raise RuntimeError('setup() must be called before variables can be accessed')

        try:
            return self._varmanager[name]
        except KeyError:
            if isinstance(name, tuple):
                name, vector = name
                if not getattr(self._varmanager.vectors(), vector, False):
                    raise NameError("'%s' is not a valid vector name" % vector)
                istuple = True
            else:
                vector = 'unknowns'
                istuple = False
            subsys, subname = name.split(':', 1)
            try:
                if istuple:
                    return self._subsystems[subsys][subname, vector]
                else:
                    return self._subsystems[subsys][subname]
            except:
                raise KeyError("Can't find variable '%s' in %s vector in system '%s'" %
                               (name, vector, self.pathname))

    def add(self, name, system, promotes=None):
        """Add a subsystem to this group, specifying its name and any variables
        that it promotes to the parent level.

        Parameters
        ----------
        name : str
            the name by which the subsystem is to be known

        system : `System`
            the subsystem to be added

        promotes : tuple, optional
            the names of variables in the subsystem which are to be promoted
        """
        if promotes is not None:
            system._promotes = promotes

        self._subsystems[name] = system
        system.name = name
        return system

    def connect(self, source, target):
        """Connect the given source variable to the given target
        variable.

        Parameters
        ----------
        source : source
            the name of the source variable

        target : str
            the name of the target variable
        """
        self._src[target] = source

    def subsystems(self, local=False):
        """ Returns an iterator over subsystems.

        local: bool
            Set to True to return only systems that are local.
        """
        if local:
            return self._local_subsystems.items()
        else:
            return self._subsystems.items()

    def subgroups(self, local=False):
        """
        Returns
        -------
        iterator
            iterator over subgroups.
        """
        for name, subsystem in self.subsystems(local=local):
            if isinstance(subsystem, Group):
                yield name, subsystem

    def components(self, local=False):
        """
        Returns
        -------
        iterator
            iterator over sub-`Component`s.
        """
        for name, comp in self.subsystems(local=local):
            if isinstance(comp, Component):
                yield name, comp

    def _setup_paths(self, parent_path):
        """Set the absolute pathname of each `System` in the tree.

        Parameter
        ---------
        parent_path : str
            the pathname of the parent `System`, which is to be prepended to the
            name of this child `System` and all subsystems.
        """
        super(Group, self)._setup_paths(parent_path)
        for name, sub in self.subsystems():
            sub._setup_paths(self.pathname)

    def _setup_variables(self):
        """Create dictionaries of metadata for parameters and for unknowns for
           this `Group` and stores them as attributes of the `Group'. The
           relative name of subsystem variables with respect to this `Group`
           system is included in the metadata.

           Returns
           -------
           tuple
               a dictionary of metadata for parameters and for unknowns
               for all subsystems
        """
        for name, sub in self.subsystems():
            subparams, subunknowns = sub._setup_variables()
            for p, meta in subparams.items():
                meta = meta.copy()
                meta['relative_name'] = self._var_pathname(meta['relative_name'], sub)
                self._params_dict[p] = meta

            for u, meta in subunknowns.items():
                meta = meta.copy()
                meta['relative_name'] = self._var_pathname(meta['relative_name'], sub)
                self._unknowns_dict[u] = meta

        return self._params_dict, self._unknowns_dict

    def _var_pathname(self, name, subsystem):
        """
        Returns
        -------
        str
            the pathname of the given variable, based on its promotion status.
        """
        if subsystem.promoted(name):
            return name
        if len(subsystem.name) > 0:
            return subsystem.name+':'+name
        else:
            return name

    def _setup_communicators(self, comm):
        """
        Assign communicator to this `Group` and all of it's subsystems

        Parameters
        ----------
        comm : an MPI communicator (real or fake)
            The communicator being offered by the parent system.
        """
        self._local_subsystems = OrderedDict()

        self.comm = get_comm_if_active(self, comm)

        if not self.is_active():
            return

        for name, sub in self.subsystems():
            sub._setup_communicators(self.comm)
            if sub.is_active():
                self._add_local_subsystem(sub)

    def _setup_vectors(self, param_owners, connections, parent_vm=None,
                       top_unknowns=None, impl=BasicImpl):
        """Create a `VarManager` for this `Group` and all below it in the
        `System` tree.

        Parameters
        ----------
        param_owners : dict
            a dictionary mapping `System` pathnames to the pathnames of parameters
            they are reponsible for propagating

        connections : dict
            a dictionary mapping the pathname of a target variable to the
            pathname of the source variable that it is connected to

        parent_vm : `VarManager`, optional
            the `VarManager` for the parent `Group`, if any, into which this
            `VarManager` will provide a view.

        top_unknowns : `VecWrapper`, optional
            the `Problem` level unknowns `VecWrapper`

        impl : an implementation factory, optional
            Specifies the factory object used to create `VecWrapper` and
            `DataXfer` objects.
        """
        if not self.is_active():
            return

        my_params = param_owners.get(self.pathname, [])
        if parent_vm is None:
            self._varmanager = VarManager(self.comm,
                                          self.pathname, self._params_dict, self._unknowns_dict,
                                          my_params, connections, impl=impl)
            top_unknowns = self._varmanager.unknowns
        else:
            self._varmanager = ViewVarManager(top_unknowns,
                                              parent_vm,
                                              self.comm,
                                              self.pathname,
                                              self._params_dict,
                                              self._unknowns_dict,
                                              my_params)

        self._views = {}
        for name, sub in self.subgroups():
            sub._setup_vectors(param_owners, connections, parent_vm=self._varmanager,
                               top_unknowns=top_unknowns)
            self._views[name] = sub._varmanager.vectors()

        for name, sub in self.components():
            self._views[name] = create_views(top_unknowns, self._varmanager, self.comm,
                                             sub.pathname,
                                             sub._params_dict, sub._unknowns_dict, [], connections)

    def _add_local_subsystem(self, sub):
        """
        Add a subsystem that is local to this process and mark corresponding
        variables as local.

        Parameters
        ----------
        sub : `System`
            `System` being added.
        """
        name = sub.name
        self._local_subsystems[name] = sub
        for name, meta in self._params_dict.items():
            if name.startswith(sub.pathname+':'):
                meta['local'] = True

        for name, meta in self._unknowns_dict.items():
            if name.startswith(sub.pathname+':'):
                meta['local'] = True

    def _get_explicit_connections(self):
        """ Returns
            -------
            dict
                explicit connections in this `Group`, represented as a mapping
                from the pathname of the target to the pathname of the source
        """
        connections = {}
        for _, sub in self.subgroups():
            connections.update(sub._get_explicit_connections())

        for tgt, src in self._src.items():
            src_pathname = get_absvarpathnames(src, self._unknowns_dict, 'unknowns')[0]
            for tgt_pathname in get_absvarpathnames(tgt, self._params_dict, 'params'):
                connections[tgt_pathname] = src_pathname

        return connections

    def solve_nonlinear(self, params=None, unknowns=None, resids=None):
        """
        Solves the group using the slotted nl_solver.

        Parameters
        ----------
        params : `VecWrapper`, optional
            ``VecWrapper` ` containing parameters (p)

        unknowns : `VecWrapper`, optional
            `VecWrapper`  containing outputs and states (u)

        resids : `VecWrapper`
            `VecWrapper`  containing residuals. (r)
        """
        if self.is_active():
            params   = params   if params   is not None else self._varmanager.params
            unknowns = unknowns if unknowns is not None else self._varmanager.unknowns
            resids   = resids   if resids   is not None else self._varmanager.resids

            self.nl_solver.solve(params, unknowns, resids, self)

    def children_solve_nonlinear(self):
        """
        Loops over our children systems and asks them to solve.
        """

        # transfer data to each subsystem and then solve_nonlinear it
        for name, sub in self.subsystems(local=True):
            self._varmanager._transfer_data(name)
            view = self._views[sub.name]
            print('solving',name,'in rank',self.comm.rank)
            sub.solve_nonlinear(view.params, view.unknowns, view.resids)
            print('done solving',name,'in rank',self.comm.rank)

    def apply_nonlinear(self, params=None, unknowns=None, resids=None):
        """
        Evaluates the residuals of our children systems.

        Parameters
        ----------
        params : `VecWrapper`, optional
            ``VecWrapper` ` containing parameters (p)

        unknowns : `VecWrapper`, optional
            `VecWrapper`  containing outputs and states (u)

        resids : `VecWrapper`
            `VecWrapper`  containing residuals. (r)
        """
        if not self.is_active():
            return

        params   = params   if params   is not None else self._varmanager.params
        unknowns = unknowns if unknowns is not None else self._varmanager.unknowns
        resids   = resids   if resids   is not None else self._varmanager.resids

        # transfer data to each subsystem and then apply_nonlinear to it
        for name, sub in self.subsystems(local=True):
            self._varmanager._transfer_data(name)
            view = self._views[sub.name]
            sub.apply_nonlinear(view.params, view.unknowns, view.resids)

    def jacobian(self, params, unknowns, resids):
        """
        Linearize all our subsystems.

        Parameters
        ----------
        params : `VecwWapper`
            `VecwWapper` containing parameters (p)

        unknowns : `VecwWapper`
            `VecwWapper` containing outputs and states (u)

        resids : `VecWrapper`
            `VecWrapper`  containing residuals. (r)
        """

        for name, system in self.subsystems(local=True):

            view = self._views[system.name]

            params   = view.params
            unknowns = view.unknowns
            resids   = view.resids

            # Instigate finite difference on child if user requests.
            if system.fd_options['force_fd'] == True:
                jacobian_cache = system.fd_jacobian(params, unknowns, resids)
            else:
                jacobian_cache = system.jacobian(params, unknowns, resids)

            if isinstance(system, Component) and \
               not isinstance(system, ParamComp):
                system._jacobian_cache = jacobian_cache

            # The user might submit a scalar Jacobian as a float.
            # It is really inconvenient if we don't allow it.
            if jacobian_cache is not None:
                for key, J in iteritems(jacobian_cache):
                    if isinstance(J, real_types):
                        jacobian_cache[key] = np.array([[J]])


    def apply_linear(self, params, unknowns, dparams, dunknowns, dresids, mode):
        """Calls apply_linear on our children. If our child is a `Component`,
        then we need to also take care of the additional 1.0 on the diagonal
        for explicit outputs.

        df = du - dGdp * dp or du = df and dp = -dGdp^T * df

        Parameters
        ----------
        params : `VecwWrapper`
            `VecwWrapper` containing parameters (p)

        unknowns : `VecwWrapper`
            `VecwWrapper` containing outputs and states (u)

        dparams : `VecwWrapper`
            `VecwWrapper` containing either the incoming vector in forward mode
            or the outgoing result in reverse mode. (dp)

        dunknowns : `VecwWrapper`
            In forward mode, this `VecwWrapper` contains the incoming vector for
            the states. In reverse mode, it contains the outgoing vector for
            the states. (du)

        dresids : `VecwWrapper`
            `VecwWrapper` containing either the outgoing result in forward mode
            or the incoming vector in reverse mode. (dr)

        mode : string
            Derivative mode, can be 'fwd' or 'rev'
        """
        if not self.is_active():
            return

        varmanager = self._varmanager

        if mode == 'fwd':
            # Full Scatter
            varmanager._transfer_data(deriv=True)

        for name, system in self.subsystems(local=True):

            view = self._views[system.name]

            params    = view.params
            unknowns  = view.unknowns
            resids    = view.resids
            dparams   = view.dparams
            dunknowns = view.dunknowns
            dresids   = view.dresids

            # Special handling for Components
            if isinstance(system, Component) and not isinstance(system, ParamComp):

                # Forward Mode
                if mode == 'fwd':

                    system.apply_linear(params, unknowns, dparams, dunknowns,
                                        dresids, mode)
                    dresids.vec *= -1.0

                    for var in dunknowns.keys():
                        dresids[var] += dunknowns.flat[var]

                # Adjoint Mode
                elif mode == 'rev':

                    # Sign on the local Jacobian needs to be -1 before
                    # we add in the fake residual. Since we can't modify
                    # the 'du' vector at this point without stomping on the
                    # previous component's contributions, we can multiply
                    # our local 'arg' by -1, and then revert it afterwards.
                    dresids.vec *= -1.0
                    system.apply_linear(params, unknowns, dparams, dunknowns,
                                        dresids, mode)
                    dresids.vec *= -1.0

                    for var in dunknowns.keys():
                        dunknowns[var] += dresids.flat[var]

            # Groups and all other systems just call their own apply_linear.
            else:
                system.apply_linear(params, unknowns, dparams, dunknowns,
                                    dresids, mode)

        if mode == 'rev':
            # Full Scatter
            varmanager._transfer_data(mode='rev', deriv=True)

    def solve_linear(self, rhs, params, unknowns, mode="auto"):
        """
        Single linear solution applied to whatever input is sitting in
        the rhs vector.

        Parameters
        ----------
        rhs: `ndarray`
            Right hand side for our linear solve.

        params : `VecwWrapper`
            `VecwWrapper` containing parameters (p)

        unknowns : `VecwWrapper`
            `VecwWrapper` containing outputs and states (u)

        mode : string
            Derivative mode, can be 'fwd' or 'rev', but generally should be
            called wihtout mode so that the user can set the mode in this
            system's ln_solver.options.
        """
        if not self.is_active():
            return

        if rhs.norm() < 1e-15:
            self.sol_vec.array[:] = 0.0
            return self.sol_vec.array

        if mode=='auto':
            mode = self.ln_solver.options['mode']

        # Solve Jacobian, df |-> du [fwd] or du |-> df [rev]
        self.rhs_buf[:] = self.rhs_vec.array[:]
        self.sol_buf[:] = self.sol_vec.array[:]
        self.sol_buf[:] = self.ln_solver.solve(self.rhs_buf, self, mode=mode)
        self.sol_vec.array[:] = self.sol_buf[:]

    def clear_dparams(self):
        """ Zeros out the dparams (dp) vector."""

        varmanager = self._varmanager
        varmanager.dparams.vec[:] = 0.0

        # Recurse to clear all dparams vectors.
        for name, system in self.subsystems(local=True):

            if isinstance(system, Component):
                view = self._views[system.name]
                view.dparams.vec[:] = 0.0

            else:
                system.clear_dparams()

    def dump(self, nest=0, file=sys.stdout, verbose=True, dvecs=False):
        """
        Writes a formated dump of the `System` tree to file.

        Parameters
        ----------
        nest : int, optional
            Starting nesting level.  Defaults to 0.

        file : an open file, optional
            Where output is written.  Defaults to sys.stdout.

        verbose : bool, optional
            If True (the default), output additional info beyond
            just the tree structure.

        dvecs : bool, optional
            If True, show contents of du and dp vectors instead of
            u and p (the default).
        """
        klass = self.__class__.__name__
        if dvecs:
            ulabel = 'du'
            plabel = 'dp'
            uvecname = 'dunknowns'
            pvecname = 'dparams'
        else:
            ulabel = 'u'
            plabel = 'p'
            uvecname = 'unknowns'
            pvecname = 'params'

        uvec = getattr(self._varmanager, uvecname)
        pvec = getattr(self._varmanager, pvecname)

        file.write("%s %s '%s'    req: %s  usize:%d  psize:%d\n" %
                     (" "*nest,
                      klass,
                      self.name,
                      self.get_req_procs(),
                      uvec.vec.size,
                      pvec.vec.size))

        vec_conns = dict(self._varmanager.data_xfer[''].vec_conns)
        byobj_conns = dict(self._varmanager.data_xfer[''].byobj_conns)

        # collect width info
        lens = [len(u)+sum(map(len,v)) for u,v in
                          chain(vec_conns.items(), byobj_conns.items())]
        if lens:
            nwid = max(lens) + 9
        else:
            lens = [len(n) for n in uvec.keys()]
            nwid = max(lens) if lens else 12

        for v, meta in uvec.items():
            if verbose:
                if meta.get('pass_by_obj'):
                    continue
                file.write(" "*(nest+8))
                uslice = '{0}[{1[0]}:{1[1]}]'.format(ulabel, uvec._slices[v])
                pnames = [p for p,u in vec_conns.items() if u==v]

                if pnames:
                    if len(pnames) == 1:
                        pname = pnames[0]
                        pslice = pvec._slices[pname]
                        pslice = '%d:%d' % (pslice[0], pslice[1])
                    else:
                        pslice = [('%d:%d' % pvec._slices[p]) for p in pnames]
                        if len(pslice) > 1:
                            pslice = ','.join(pslice)
                        else:
                            pslice = pslice[0]

                    pslice = '{}[{}]'.format(plabel, pslice)

                    connstr = '%s -> %s' % (v, pnames)
                    file.write("{0:<{nwid}} {1:<10} {2:<10} {3:>10}\n".format(connstr,
                                                                    uslice,
                                                                    pslice,
                                                                    repr(uvec[v]),
                                                                    nwid=nwid))
                else:
                    file.write("{0:<{nwid}} {1:<21} {2:>10}\n".format(v,
                                                                  uslice,
                                                                  repr(uvec[v]),
                                                                  nwid=nwid))

        if not dvecs:
            for dest, src in byobj_conns.items():
                file.write(" "*(nest+8))
                connstr = '%s -> %s:' % (src, dest)
                file.write("{0:<{nwid}} (by_obj)  ({1})\n".format(connstr,
                                                                  repr(uvec[src]),
                                                                  nwid=nwid))

        # now do the Components
        nest += 3
        for name, sub in self.subsystems(local=True):
            if isinstance(sub, Component):
                uvec = getattr(self._views[name], uvecname)
                file.write("%s %s '%s'    req: %s  usize:%d\n" %
                           (" "*nest,
                            sub.__class__.__name__,
                            name,
                            sub.get_req_procs(),
                            uvec.vec.size))
                for v, meta in uvec.items():
                    if verbose:
                        if v in uvec._slices:
                            uslice = '{0}[{1[0]}:{1[1]}]'.format(ulabel, uvec._slices[v])
                            file.write("{0}{1:<{nwid}} {2:<21} {3:>10}\n".format(" "*(nest+8),
                                                                             v,
                                                                             uslice,
                                                                             repr(uvec[v]),
                                                                             nwid=nwid))
                        elif not dvecs: # deriv vecs don't have passing by obj
                            file.write("{0}{1:<{nwid}}  (by_obj) ({2})\n".format(" "*(nest+8),
                                                                                 v,
                                                                                 repr(uvec[v]),
                                                                                 nwid=nwid))
            else:
                sub.dump(nest, file=file, verbose=verbose, dvecs=dvecs)

    def get_req_procs(self):
        """
        Returns
        -------
        tuple
            A tuple of the form (min_procs, max_procs), indicating the min and max
            processors usable by this `Group`
        """
        min_procs = 1
        max_procs = 1

        for name, sub in self.subsystems():
            sub_min, sub_max = sub.get_req_procs()
            min_procs = max(min_procs, sub_min)
            if max_procs is not None:
                if sub_max is None:
                    max_procs = None
                else:
                    max_procs = max(max_procs, sub_max)

        return (min_procs, max_procs)

    def _update_sub_unit_conv(self, parent_params_dict=None):
        """
        Propagate unit conversion factors down the system tree.
        """
        if parent_params_dict:
            for name, meta in self._params_dict.items():
                pmeta = parent_params_dict.get(name)
                if pmeta and 'unit_conv' in pmeta:
                    meta['unit_conv'] = pmeta['unit_conv']

        for name, sub in self.subgroups():
            sub._update_sub_unit_conv(self._params_dict)

def _get_implicit_connections(params_dict, unknowns_dict):
    """
    Finds all matches between relative names of parameters and
    unknowns.  Any matches imply an implicit connection.  All
    connections are expressed using absolute pathnames.

    This should only be called using params and unknowns from the
    top level `Group` in the system tree.

    Parameters
    ----------
    params_dict : dict
        dictionary of metadata for all parameters in this `Group`

    unknowns_dict : dict
        dictionary of metadata for all unknowns in this `Group`

    Returns
    -------
    dict
        implicit connections in this `Group`, represented as a mapping
        from the pathname of the target to the pathname of the source

    Raises
    ------
    RuntimeError
        if a a promoted variable name matches multiple unknowns
    """

    # collect all absolute names that map to each relative name
    abs_unknowns = {}
    for abs_name, u in unknowns_dict.items():
        abs_unknowns.setdefault(u['relative_name'], []).append(abs_name)

    abs_params = {}
    for abs_name, p in params_dict.items():
        abs_params.setdefault(p['relative_name'], []).append(abs_name)

    # check if any relative names correspond to mutiple unknowns
    for name, lst in abs_unknowns.items():
        if len(lst) > 1:
            raise RuntimeError("Promoted name '%s' matches multiple unknowns: %s" %
                               (name, lst))

    connections = {}
    for uname, uabs in abs_unknowns.items():
        pabs = abs_params.get(uname, ())
        for p in pabs:
            connections[p] = uabs[0]

    return connections


def get_absvarpathnames(var_name, var_dict, dict_name):
    """
    Parameters
    ----------
    var_name : str
        name of a variable relative to a `System`

    var_dict : dict
        dictionary of variable metadata, keyed on relative name

    dict_name : str
        name of var_dict (used for error reporting)

    Returns
    -------
    list of str
        the absolute pathnames for the given variables in the
        variable dictionary that map to the given relative name.
    """
    pnames = []
    for pathname, meta in var_dict.items():
        if meta['relative_name'] == var_name:
            pnames.append(pathname)

    if not pnames:
        raise RuntimeError("'%s' not found in %s" % (var_name, dict_name))

    return pnames
