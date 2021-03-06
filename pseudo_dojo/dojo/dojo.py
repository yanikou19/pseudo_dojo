from __future__ import division, print_function

import os
import abc
import shutil
import time
import numpy as np

from pprint import pprint
from pymatgen.serializers.json_coders import json_pretty_dump
from pymatgen.io.abinitio.pseudos import Pseudo
from pymatgen.io.abinitio.launcher import PyResourceManager
from pymatgen.io.abinitio.calculations import PPConvergenceFactory
from pseudo_dojo.dojo.deltaworks import DeltaFactory


class DojoError(Exception):
    """Base Error class for DOJO calculations."""


class Dojo(object):
    """
    This object drives the execution of the tests for the pseudopotential.

    A Dojo has a set of masters, each master is associated to a particular trial
    and is responsible for the validation/rating of the results of the tests.
    """
    Error = DojoError

    def __init__(self, manager, max_ncpus=1, max_level=None, verbose=0):
        """
        Args:
            manager:
                `TaskManager` object that will handle the sumbmission of the job 
                and the parallel execution.
            max_ncpus:
                Max number of CPUs to use
            max_level:
                Max test level to perform.
            verbose:
                Verbosity level (int).
        """
        self.manager = manager
        self.max_ncpus = max_ncpus
        self.verbose = verbose

        # List of master classes that will be instanciated afterwards.
        # They are ordered according to the master level.
        classes = [m for m in DojoMaster.__subclasses__()]
        classes.sort(key=lambda cls: cls.dojo_level)

        self.master_classes = classes

        if max_level is not None:
            self.master_classes = classes[:max_level+1]

    def __str__(self):
        return repr_dojo_levels()

    def challenge_pseudo(self, pseudo, **kwargs):
        """
        This method represents the main entry point for client code.
        The Dojo receives a pseudo-like object and delegate the execution
        of the tests to the dojo_masters

        Args:
            `Pseudo` object or filename.
        """
        pseudo = Pseudo.aspseudo(pseudo)

        workdir = "DOJO_" + pseudo.name

        # Build master instances.
        masters = [cls(manager=self.manager, max_ncpus=self.max_ncpus,
                       verbose=self.verbose) for cls in self.master_classes]
        isok = False
        for master in masters:
            if master.accept_pseudo(pseudo, **kwargs):
                isok = master.start_training(workdir, **kwargs)
                if not isok:
                    print("master: %s returned isok %s.\n Skipping next trials!" % (master.name, isok))
                    break

        return isok


class DojoMaster(object):
    """"
    Abstract base class for the dojo masters.
    Subclasses must define the class attribute level.
    """
    __metaclass__ = abc.ABCMeta

    Error = DojoError

    def __init__(self, manager, max_ncpus=1, verbose=0):
        """
        Args:
            manager:
                `TaskManager` object 
            max_ncpus:
                Max number of CPUs to use
            verbose:
                Verbosity level (int).
        """
        self.manager = manager
        self.max_ncpus = max_ncpus
        self.verbose = verbose

    @property
    def name(self):
        """Name of the subclass."""
        return self.__class__.__name__

    @staticmethod
    def subclass_from_dojo_level(dojo_level):
        """Returns a subclass of `DojoMaster` given the dojo_level."""
        classes = []
        for cls in DojoMaster.__subclasses__():
            if cls.dojo_level == dojo_level:
                classes.append(cls)

        if len(classes) != 1:
            raise self.Error("Found %d masters with dojo_level %d" % (len(classes), dojo_level))

        return classes[0]

    def inspect_pseudo(self, pseudo):
        """Returns the maximum level of the DOJO trials passed by the pseudo."""
        if not pseudo.dojo_report:
            return None
        else:
            levels = [dojo_key2level(key) for key in pseudo.dojo_report]
            return max(levels)

    def accept_pseudo(self, pseudo, **kwargs):
        """
        Returns True if the mast can train the pseudo.
        This method is called before testing the pseudo.

        A master can train the pseudo if his level == pseudo.dojo_level + 1
        """
        if not isinstance(pseudo, Pseudo):
            pseudo = Pseudo.from_filename(pseudo)

        ready = False
        pseudo_dojo_level = self.inspect_pseudo(pseudo)
        
        if pseudo_dojo_level is None:
            # Hints are missing
            ready = (self.dojo_level == 0)
        else:
            if pseudo_dojo_level == self.dojo_level:
                # pseudo has already a test associated to this level.
                # check if it has the same accuracy.
                accuracy = kwargs.get("accuracy", "normal")
                if accuracy not in pseudo.dojo_report[self.dojo_key]:
                    ready = True
                else:
                    print("%s: %s has already an entry for accuracy %s" % (self.name, pseudo.name, accuracy))
                    ready = False

            else:
                # Pseudo level must be one less than the level of the master.
                ready = (pseudo_dojo_level == self.dojo_level - 1)

        if not ready:
            print("%s: Sorry, %s-san, I cannot train you" % (self.name, pseudo.name))
        else:
            print("%s: Welcome %s-san, I'm your level-%d trainer" % (self.name, pseudo.name, self.dojo_level))
            self.pseudo = pseudo

        return ready

    @abc.abstractmethod
    def challenge(self, workdir, **kwargs):
        """Abstract method to run the calculation."""

    @abc.abstractmethod
    def make_report(self, **kwargs):
        """
        Abstract method.
        Returns: 
            report:
                Dictionary with the results of the trial.
        """

    def write_dojo_report(self, report, overwrite_data=False, ignore_errors=False):
        """
        Write/update the DOJO_REPORT section of the pseudopotential.
        """
        dojo_key = self.dojo_key
        pseudo = self.pseudo

        # Read old_report from pseudo.
        old_report = pseudo.read_dojo_report()

        if dojo_key not in old_report:
            # Create new entry
            old_report[dojo_key] = {}
        else:
            # Check that we are not going to overwrite data.
            if self.accuracy in old_report[dojo_key] and not overwrite_data:
                raise self.Error("%s already exists in the old pseudo. Cannot overwrite data" % dojo_key)

        # Update old report card with the new one.
        old_report[dojo_key].update(report[dojo_key])

        # Write new report
        pseudo.write_dojo_report(old_report)

    def start_training(self, workdir, **kwargs):
        """Start the tests in the working directory workdir."""
        start_time = time.time()
        results = self.challenge(workdir, **kwargs)

        report = self.make_report(results, **kwargs)

        json_pretty_dump(results, os.path.join(workdir, "report.json"))

        self.write_dojo_report(report)

        print("Elapsed time %.2f [s]" % (time.time() - start_time))

        isok = True
        if "_exceptions" in report:
            isok = False
            print("got exceptions: ",report["_exceptions"])

        return isok


class HintsMaster(DojoMaster):
    """
    Level 0 master that analyzes the convergence of the total energy versus
    the plane-wave cutoff energy.
    """
    dojo_level = 0
    dojo_key = "hints"

    # Absolute tolerance for low,normal,high accuracy.
    _ATOLS_MEV = (10, 1, 0.1)

    def challenge(self, workdir, **kwargs):
        pseudo = self.pseudo
        toldfe = 1.e-8

        factory = PPConvergenceFactory()

        workdir = os.path.join(workdir, "LEVEL_" + str(self.dojo_level))

        estep = kwargs.get("estep", 10)

        eslice = slice(5, None, estep)

        w = factory.work_for_pseudo(workdir, self.manager, pseudo, eslice,
                                    toldfe=toldfe, atols_mev=self._ATOLS_MEV)

        if os.path.exists(w.workdir):
            shutil.rmtree(w.workdir)

        print("Converging %s in iterative mode with ecut_slice %s, max_ncpus = %d ..." %
              (pseudo.name, eslice, self.max_ncpus))

        w.start()
        w.wait()

        wres = w.get_results()
        w.move("ITERATIVE")

        estart = max(wres["low"]["ecut"] - estep, 5)
        if estart <= 10:
            estart = 1 # To be sure we don't overestimate ecut_low

        estop, estep = wres["high"]["ecut"] + estep, 1

        erange = list(np.arange(estart, estop, estep))

        work = factory.work_for_pseudo(workdir, self.manager, pseudo, erange,
                                       toldfe=toldfe, atols_mev=self._ATOLS_MEV)

        print("Finding optimal values for ecut in the range [%.1f, %.1f, %1.f,] Hartree, "
              "max_ncpus = %d ..." % (estart, estop, estep, self.max_ncpus))

        PyResourceManager(work, self.max_ncpus).run()

        wf_results = work.get_results()

        #wf_results.json_dump(work.path_in_workdir("dojo_results.json"))

        return wf_results

    def make_report(self, results, **kwargs):
        d = {}
        for key in ["low", "normal", "high"]:
            d[key] = results[key]

        if results.exceptions:
            d["_exceptions"] = str(results.exceptions)

        return {self.dojo_key: d}


class DeltaFactorMaster(DojoMaster):
    """
    Level 1 master that drives the computation of the delta factor.
    """
    dojo_level = 1
    dojo_key = "delta_factor"

    def accept_pseudo(self, pseudo, **kwargs):
        """Returns True if the master can train the pseudo."""
        ready = super(DeltaFactorMaster, self).accept_pseudo(pseudo, **kwargs)

        # Do we have this element in the deltafactor database?
        from pseudo_dojo.refdata.deltafactor import df_database
        return (ready and df_database().has_symbol(self.pseudo.symbol))

    def challenge(self, workdir, **kwargs):
        self.accuracy = kwargs.pop("accuracy", "normal")

        factory = DeltaFactory()

        # Calculations will be executed in this directory.
        workdir = os.path.join(workdir, "LEVEL_" + str(self.dojo_level) + "_ACC_" + self.accuracy)

        # 6750 is the value used in the deltafactor code.
        kppa = kwargs.get("kppa", 6750)
        #kppa = 1

        if self.verbose:
            print("Running delta_factor calculation with %d python threads" % self.max_ncpus)
            print("Will use kppa = %d " % kppa)
            print("Accuracy = %s" % self.accuracy)
            print("Manager = ",self.manager)

        work = factory.work_for_pseudo(workdir, self.manager, self.pseudo, 
                                       accuracy=self.accuracy, kppa=kppa, ecut=None)

        retcodes = PyResourceManager(work, self.max_ncpus).run()

        if self.verbose:
            print("Returncodes %s" % retcodes)

        wf_results = work.get_results()

        wf_results.json_dump(work.path_in_workdir("dojo_results.json"))
        return wf_results

    def make_report(self, results, **kwargs):
        # Get reference results (Wien2K).
        from pseudo_dojo.refdata.deltafactor import df_database, df_compute
        wien2k = df_database().get_entry(self.pseudo.symbol)

        # Get our results and compute deltafactor estimator.
        v0, b0_GPa, b1 = results["v0"], results["b0_GPa"], results["b1"]

        dfact = df_compute(wien2k.v0, wien2k.b0_GPa, wien2k.b1, v0, b0_GPa, b1, b0_GPa=True)
        print("Deltafactor = %.3f meV" % dfact)

        d = dict(v0=v0,
                 b0_GPa=b0_GPa,
                 b1=b1,
                 dfact=dfact
                )

        if results.exceptions:
            d["_exceptions"] = str(results.exceptions)

        d = {self.accuracy: d}
        return {self.dojo_key: d}

################################################################################

_key2level = {}
for cls in DojoMaster.__subclasses__():
    _key2level[cls.dojo_key] = cls.dojo_level


def dojo_key2level(key):
    """Return the trial level from the name found in the pseudo."""
    return _key2level[key]


def repr_dojo_levels():
    """String representation of the different levels of the Dojo."""
    level2key = {v: k for k,v in _key2level.items()}

    lines = ["Dojo level --> Challenge"]
    for k in sorted(level2key):
        lines.append("level %d --> %s" % (k, level2key[k]))
    return "\n".join(lines)

