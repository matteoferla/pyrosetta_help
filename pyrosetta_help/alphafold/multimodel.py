__all__ = ['AF2NotebookAnalyser']

import json
import os
import re
from typing import (Optional, Dict, List, Sequence)

import numpy as np
import pandas as pd
import pyrosetta

pr_rs = pyrosetta.rosetta.core.select.residue_selector
from .retrieval import reshape_errors
from .constraints import add_pae_constraints, add_interchain_pae_constraints
from ..common_ops import pose_from_file
import pickle, warnings
from collections import defaultdict


class AF2NotebookAnalyser:
    """
    This class features heavily in the notebook ``colab-pyrosetta-dimer.ipynb``
    It allows the analysis of the results of the AF2 protocol for a dimer.
    Namely, ``.sidechain_relax``

    .. code-block:: python

        analyser = ph.AF2NotebookAnalyser(folder=folder_name, load_poses=True)
        analyser.sidechain_relax(cycles)
        analyser.constrain(tolerance=2)
        analyser.relax(cycles)
        analyser.calculate_interface()
        score: pd.DataFrame = analyser.scores

    The ``.scores`` attribute is a pandas DataFrame generated by ``.make_AF2_dataframe``

    One can also make a phosphorylated model

    .. code-block:: python

        pdb_ptms = analyser.parse_phosphosite(raw, maximum=analyser.original_poses[1].chain_end(1))
        analyser.make_phosphorylated(pdb_ptms, chain, cycles)

    To access specific poses the following attributes are available:

    .. code-block:: python

        print(analyser.pose_groupnames) # ['relaxed', 'original', 'phospho']
        analyser.original_poses[i]
        analyser.relaxed_poses[i]
        analyser.phospho_poses[i]

    """

    def __init__(self, folder: str, load_poses: bool = True):
        self.folder = folder
        self.scores: pd.DataFrame = self.make_AF2_dataframe()
        self._add_settings()
        self.relaxed_poses = dict()
        self.original_poses = dict()
        self.phospho_poses = dict()
        self.pose_groupnames = ['relaxed', 'original', 'phospho']
        if load_poses:
            self.original_poses = self.get_poses()
        self.errors = self.get_errors()

    @property
    def poses(self):
        # to alter change pose_groupnames and add the required *_poses attributes, I guess.
        return {groupname: getattr(self, groupname + '_poses') for groupname in self.pose_groupnames}

    def make_AF2_dataframe(self) -> pd.DataFrame:
        """
        Given a folder form ColabsFold return a dictionary
        with key rank index
        and value a dictionary of details

        This is convoluted, but it may have been altered by a human.
        """
        filenames = os.listdir(self.folder)
        # group files
        ranked_filenames = {}
        for filename in filenames:
            if not re.search('rank_\d+.*\.pdb', filename):
                continue
            # kcc2dimer_42460_unrelaxed_rank_1_model_5.pdb
            # rex = re.match('rank_(\d+)_model_(\d+).*seed_(\d+)(.*)\.pdb', filename)
            rex = re.search('(?P<name>.*)_(?P<seed>\d+)_(?P<state>\w+)_rank_(?P<rank>\d+)_model_(?P<model>\d+)\.pdb',
                            filename)
            name = rex.group('name')
            state = 'unrelaxed' not in rex.group('state')
            rank = int(rex.group('rank'))
            model = int(rex.group('model'))
            seed = int(rex.group('seed'))
            if rank in ranked_filenames and ranked_filenames[rank]['relaxed']:
                continue
            data = dict(name=filename,
                        path=os.path.join(self.folder, filename),
                        rank=rank,
                        model=model,
                        seed=seed,
                        relaxed=state
                        )
            ranked_filenames[rank] = data
        # make dataframe
        return pd.DataFrame(list(ranked_filenames.values()))

    def _add_settings(self):
        # add data from settings.
        pLDDTs = {}
        pTMscores = {}
        settings_filename = os.path.join(self.folder, 'settings.txt')
        if not os.path.exists(settings_filename):
            warnings.warn(f'{settings_filename} missing!')
            self.scores['pLDDT'] = self.scores['rank'].apply(lambda r: 100)
            self.scores['pTMscore'] = self.scores['rank'].apply(lambda r: 100)
        with open(settings_filename, 'r') as fh:
            for match in re.findall('rank_(\d+).*pLDDT\:(\d+\.\d+)\ pTMscore:(\d+\.\d+)', fh.read()):
                pLDDTs[int(match[0])] = float(match[1])
                pTMscores[int(match[0])] = float(match[2])
        self.scores['pLDDT'] = self.scores['rank'].map(pLDDTs)
        self.scores['pTMscore'] = self.scores['rank'].map(pTMscores)

    def get_poses(self) -> Dict[int, pyrosetta.Pose]:
        """
        This used to be a ``pyrosetta.rosetta.utility.vector1_core_pose_Pose``
        but it turns out that the getitem of ``vector1_core_pose_Pose``
        returns a clone of the pose, not the pose itself.

        :return:
        """
        # poses = pyrosetta.rosetta.utility.vector1_core_pose_Pose(len(self.scores))
        poses = dict()
        for i, row in self.scores.iterrows():
            poses[row['rank']] = pyrosetta.pose_from_file(row['path'])
        return poses

    def get_errors(self) -> dict:
        """
        The errors are the result of reshaping via ``ph.reshape_errors``
        :return:
        """
        errors = dict()
        for i, row in self.scores.iterrows():
            filename = row['path'].replace('.pdb', '_scores.json')
            # .replace('_unrelaxed', '_pae') \
            # .replace('_relaxed', '_pae')
            # kcc2dimer_42460_unrelaxed_rank_1_model_5_scores.json
            with open(filename, 'r') as fh:
                errors[row['rank']] = np.array(json.load(fh)['pae'])
        return errors

    def _generator_poses(self, groupname: str = 'relaxed'):
        """
        :return: rank integer, pose, error dictionary
        """
        assert len(self.poses[groupname]), f'The group {groupname} is not loaded'
        return ((index, self.poses[groupname][index], self.errors[index]) for index in self.errors)

    def constrain(self, groupname: str = 'relaxed', **add_pae_constraints_arguments):
        """
        Calls the ``add_pae_constraints`` and ``add_interchain_pae_constraints`` functions on each pose in the group.
        :param groupname:
        :param add_pae_constraints_arguments:
        :return:
        """
        if len(self.original_poses) == 0:
            raise ValueError('Load poses first.')
        for index, pose, error in self._generator_poses(groupname):
            add_pae_constraints(pose, error, **add_pae_constraints_arguments)
            add_interchain_pae_constraints(pose, error, cutoff=15)

    def sidechain_relax(self, cycles: int = 5):
        """
        This is a prelude to full relaxation. This is relaxes only the sidechains,
        which if strained or clashing may result in the whole pose blowing up.

        :param cycles: cycles of FastRelax
        :return:
        """
        vanilla_scorefxn = pyrosetta.get_fa_scorefxn()
        ap_st = pyrosetta.rosetta.core.scoring.ScoreType.atom_pair_constraint
        vanilla_scorefxn.set_weight(ap_st, 0)
        movemap = pyrosetta.MoveMap()
        movemap.set_bb(False)
        movemap.set_chi(True)
        chirelax = pyrosetta.rosetta.protocols.relax.FastRelax(vanilla_scorefxn, cycles)
        chirelax.set_movemap(movemap)
        for index, pose, error in self._generator_poses('original'):
            if index not in self.relaxed_poses:
                self.relaxed_poses[index] = pose.clone()
            chirelax.apply(self.relaxed_poses[index])

    def relax(self, cycles: int = 3):
        """
        This does backbone and sidechain relaxation.
        For only sidechain relaxation, use ``.sidechain_relax``.

        :param cycles:
        :return:
        """
        if self.original_poses is None:
            raise ValueError('Load poses first.')
        scorefxn = pyrosetta.get_fa_scorefxn()
        ap_st = pyrosetta.rosetta.core.scoring.ScoreType.atom_pair_constraint
        scorefxn.set_weight(ap_st, 1)
        for index, pose, error in self._generator_poses('original'):
            if index not in self.relaxed_poses:
                self.relaxed_poses[index] = pose.clone()
            relax = pyrosetta.rosetta.protocols.relax.FastRelax(scorefxn, cycles)
            movemap = pyrosetta.MoveMap()
            movemap.set_bb(True)
            movemap.set_chi(True)
            movemap.set_jump(True)
            relax.set_movemap(movemap)
            relax.apply(self.relaxed_poses[index])
        # Add dG
        scorefxn.set_weight(ap_st, 0)
        self.scores['dG'] = self.scores['rank'].apply(lambda rank: scorefxn(self.relaxed_poses[rank]))

    def constrain_and_relax(self, cycles: int = 3):
        """
        Adds constraints and relaxes the poses.
        """
        self.sidechain_relax()
        self.constrain()
        self.relax(cycles)

    def calculate_interface(self, interface='A_B'):
        """
        Calculates the interface strength between the two chains.

        It does a median of the pLDDT scores of the interface residues.



        :param interface: A Rosetta format string for the description of the interface, e.g. 'A_B'
        :return:
        """
        medians: Dict[int, float] = self.get_median_interface_bfactors()  # bfactor = pLDDT
        # interface score.
        if 'dG' not in self.scores:
            return
        newdata = []
        for rank in self.scores['rank']:
            ia = pyrosetta.rosetta.protocols.analysis.InterfaceAnalyzerMover(interface)
            ia.apply(self.relaxed_poses[rank])
            newdata.append({'median_interface_pLDDT':     medians[rank],
                            'complex_energy':             ia.get_complex_energy(),
                            'separated_interface_energy': ia.get_separated_interface_energy(),
                            'complexed_sasa':             ia.get_complexed_sasa(),
                            'crossterm_interface_energy': ia.get_crossterm_interface_energy(),
                            'interface_dG':               ia.get_interface_dG(),
                            'interface_delta_sasa':       ia.get_interface_delta_sasa()})
        # adding multiple columns, hence why not apply route.
        # the order is not chanced, so all good
        newdata = pd.DataFrame(newdata)
        for column in newdata.columns:
            self.scores[column] = newdata[column]

    # ------------------------------------------------------------------------

    def _parse_folder_argument(self, folder):
        if folder and not os.path.exists(folder):
            # user specified non existant folder.
            os.mkdir(folder)
            return folder
        elif folder:
            return folder
        else:
            # use original folder.
            return self.folder

    def dump_pdbs(self,
                  groupname: str = 'relaxed',
                  folder: Optional[str] = None,
                  prefix: Optional[str] = ''):
        """
        Save in the format ``f'{prefix}rank_{index}_pyrosetta_{groupname}.pdb'``.
        """
        for index, pose, error in self._generator_poses(groupname):
            folder = self._parse_folder_argument(folder)
            path = os.path.join(folder, f'{prefix}rank_{index}_pyrosetta_{groupname}.pdb')
            pose.dump_pdb(path)

    def dump(self, folder: Optional[str] = None):
        """
        Saves ALL the pdbs (via ``dump_pdbs``) and the errors.
        """
        folder = self._parse_folder_argument(folder)
        self.scores.to_csv(os.path.join(folder, 'scores.csv'))
        self.dump_pdbs(folder=folder)
        with open(os.path.join(folder, 'errors.p', 'w')) as fh:
            pickle.dump(self.errors, fh)
        for groupname in self.poses:
            if len(self.poses[groupname]) != 0:
                self.dump_pdbs(groupname=groupname, folder=folder)

    @classmethod
    def load(cls, folder: str, load_poses=False, params=()):
        """
        Loads the poses (from PDB files), scores and errors from the folder.
        :return:
        """
        self = cls(folder=folder, load_poses=load_poses)
        self.scores = pd.read_csv(os.path.join(folder, 'scores.csv'), index_col=0)
        valids = [filename for filename in os.listdir(folder) if '.pdb' in filename]
        ranker = lambda filename: int(re.search(r'rank(\d+)\.pdb', filename).group(1))
        self.relaxed_poses = {ranker(filename): pose_from_file(filename, params) for filename in valids}
        with open(os.path.join(folder, 'errors.p', 'r')) as fh:
            self.errors = pickle.load(fh)
        return self

    # ----------------------------------------------------

    def get_interactions(self,
                         pose: pyrosetta.Pose,
                         chain_id: int,
                         threshold: float = 3.) -> pr_rs.ResidueVector:
        """
        Get the sequence of pose residue indices that are within the threshold distance of the chain_id.
        """
        chain_sele = pr_rs.ChainSelector(chain_id)
        other_chains_sele = pr_rs.NotResidueSelector(chain_sele)
        cc_sele = pr_rs.CloseContactResidueSelector()
        cc_sele.central_residue_group_selector(other_chains_sele)
        cc_sele.threshold(float(threshold))
        other_cc_sele = pr_rs.AndResidueSelector(chain_sele, cc_sele)
        return pr_rs.ResidueVector(other_cc_sele.apply(pose))

    def find_interface_residues(self):
        """
        Calls ``get_interactions`` for each pose and saves the result in the ``scores`` dataframe.
        """
        if 'N_interchain_residues_1' in self.scores:
            return  # already run
        assert self.relaxed_poses, 'No poses loaded yet!'
        assert self.relaxed_poses[1].num_chains() > 1, 'Single chain!'
        self.scores['interchain_residues_1'] = self.scores['rank'].apply(
            lambda rank: self.get_interactions(self.relaxed_poses[rank], 1))
        self.scores['interchain_residues_2'] = self.scores['rank'].apply(
            lambda rank: self.get_interactions(self.relaxed_poses[rank], 2))
        self.scores['N_interchain_residues_1'] = self.scores['interchain_residues_1'].apply(len)
        self.scores['N_interchain_residues_2'] = self.scores['interchain_residues_2'].apply(len)

    def _get_median_interface_bfactor(self, pose, residues: Sequence[int]) -> float:
        """
        Not bfactor but pLDDT score.
        """
        pbd_info = pose.pdb_info()
        bfactors = [pbd_info.bfactor(r, pose.residue(r).atom_index('CA')) for r in residues]
        return np.median(bfactors)

    def get_median_interface_bfactors(self) -> Dict[int, float]:
        """
        "bfactors" aren't actually bfactors, but pLDDT values.
        Based on what are the residues identified with ``find_interface_residues``
        in calculates the median pLDDT of these.
        """
        self.find_interface_residues()
        medians = {}
        for i, row in self.scores.iterrows():
            residues = list(row.interchain_residues_1) + list(row.interchain_residues_2)
            # ``row.rank`` is a method, just like the attribute ``row.name`` so the attribute way is not okay here:
            pose = self.relaxed_poses[row['rank']]
            medians[row['rank']] = self._get_median_interface_bfactor(pose, residues)
        return medians

    # -------------------------------------------------

    @classmethod
    def parse_phosphosite(cls, raw: str, minimum: int = 1, maximum: int = -1):
        """
        A rubbish method to convert copy-pasted Phosphosite web table.

        :param raw:
        :param minimum:
        :param maximum:
        :return:
        """
        if maximum < 1:
            maximum = float('nan')
        assert '-p' in raw, f'Is this what you wanted in your clipboard: {raw}'
        ptms = defaultdict(list)
        for resn, resi, mod in re.findall('(\w)(\d+)\-(\w+)', raw):
            if int(resi) < minimum or int(resi) > maximum:
                continue
            ptms[mod].append(int(resi))
        return ptms

    def _apply_patch(self, pose, patch, residues, restriction=None):
        MutateResidue = pyrosetta.rosetta.protocols.simple_moves.MutateResidue
        valids = []
        for r in residues:
            if r == 0:
                continue  # missing
            present_res = pose.residue(r).name3()
            new_res = f"{present_res.upper()}:{patch}"
            if restriction is not None and present_res.upper() != restriction.upper():
                continue
            MutateResidue(target=r, new_res=new_res).apply(pose)
            valids.append(r)
        return valids

    def _phospho_pose(self, pose, scorefxn, cycles=3, **modifications):
        valids = []
        for ptm, residues in modifications.items():
            if ptm == 'ub':
                continue  # What is a proxy for ubiquitination??
            elif ptm == 'p':
                v = self._apply_patch(pose,
                                      patch='phosphorylated',
                                      residues=residues)
                valids.extend(v)
            elif ptm == 'ac':
                v = self._apply_patch(pose,
                                      patch='acetylated',
                                      residues=residues)
                valids.extend(v)
            elif ptm == 'm1':
                v = self._apply_patch(pose,
                                      patch='monomethylated',
                                      residues=residues,
                                      restriction='LYS')  # monomethylarginine (NMM) will segfault
                valids.extend(v)
            elif ptm == 'm2':
                v = self._apply_patch(pose,
                                      patch='dimethylated',
                                      residues=residues,
                                      restriction='LYS')  # dimethylarginine (DA2) will segfault
                valids.extend(v)
            elif ptm == 'm3':
                v = self._apply_patch(pose,
                                      patch='trimethylated',
                                      residues=residues,
                                      restriction='LYS')  # is trimethylarginine a thing?
                valids.extend(v)
            else:
                continue  # no Gal
                # raise ValueError(f'What is {ptm}?')
        # minimise
        resi_sele = pyrosetta.rosetta.core.select.residue_selector.ResidueIndexSelector()
        for r in valids:
            resi_sele.append_index(r)
        if len(valids) == 0:
            return valids
        neigh_sele = pyrosetta.rosetta.core.select.residue_selector.NeighborhoodResidueSelector(resi_sele, 7, True)
        neighbor_vector = neigh_sele.apply(pose)
        relax = pyrosetta.rosetta.protocols.relax.FastRelax(scorefxn, cycles)
        movemap = pyrosetta.MoveMap()
        movemap.set_bb(neighbor_vector)
        movemap.set_chi(neighbor_vector)
        relax.set_movemap(movemap)
        relax.apply(pose)
        return valids

    def make_phosphorylated(self,
                            pdb_ptms: dict,
                            chain: str = 'A',
                            cycles: int = 3):
        """
        Make a phosphorylated pose from the dictionary of Phoshositeplus annotations
        (see ``parse_phosphosite``)
        """
        # convert PTM to pose
        pdb_info = self.original_poses[1].pdb_info()
        convert = lambda pdb_residues: [pdb_info.pdb2pose(res=r, chain=chain) for r in pdb_residues]
        ptms = {k: convert(pdb_residues) for k, pdb_residues in pdb_ptms.items()}
        # scorefunction
        scorefxn = pyrosetta.get_fa_scorefxn()
        ap_st = pyrosetta.rosetta.core.scoring.ScoreType.atom_pair_constraint
        scorefxn.set_weight(ap_st, 1)
        for index, pose, error in self._generator_poses('relaxed'):
            self.phospho_poses[index] = pose.clone()
            self._phospho_pose(self.phospho_poses[index], scorefxn, cycles=cycles, **ptms)
