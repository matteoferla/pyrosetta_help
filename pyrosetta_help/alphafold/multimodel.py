__all__ = ['AF2NotebookAnalyser']

import numpy as np
import os, re, json
from typing import *
import pandas as pd
import pyrosetta

pr_rs = pyrosetta.rosetta.core.select.residue_selector
from .retrieval import reshape_errors
from .constraints import add_pae_constraints
from ..common_ops import pose_from_file
import pickle

class AF2NotebookAnalyser:

    def __init__(self, folder: str, load_poses: bool = True):
        self.folder = folder
        self.scores = self.make_AF2_dataframe()
        self._add_settings()
        self.relaxed_poses = dict()
        self.original_poses = dict()
        self.phospho_poses = dict()
        self.poses = dict(relaxed=self.relaxed_poses,
                          original=self.original_poses,
                          phospho=self.phospho_poses)
        if load_poses:
            self.original_poses = self.get_poses()
        self.errors = self.get_errors()

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
            if not re.match('rank_\d+.*\.pdb', filename):
                continue
            rex = re.match('rank_(\d+)_model_(\d+).*seed_(\d+)(.*)\.pdb', filename)
            rank = int(rex.group(1))
            model = int(rex.group(2))
            seed = int(rex.group(3))
            other = rex.group(4)
            if rank in ranked_filenames and ranked_filenames[rank]['relaxed'] == True:
                continue
            data = dict(name=filename,
                        path=os.path.join(self.folder, filename),
                        rank=rank,
                        model=model,
                        seed=seed,
                        relaxed='_relaxed' in other
                        )
            ranked_filenames[rank] = data
        # make dataframe
        return pd.DataFrame(list(ranked_filenames.values()))

    def _add_settings(self):
        # add data from settings.
        pLDDTs = {}
        pTMscores = {}
        with open(os.path.join(self.folder, 'settings.txt'), 'r') as fh:
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
        errors = dict()
        for i, row in self.scores.iterrows():
            filename = row['path'].replace('.pdb', '.json') \
                .replace('_unrelaxed', '_pae') \
                .replace('_relaxed', '_pae')
            with open(filename, 'r') as fh:
                errors[row['rank']] = reshape_errors(json.load(fh))
        return errors

    def _generator_poses(self, groupname: str = 'relaxed'):
        """
        :return: rank integer, pose, error dictionary
        """
        assert len(self.poses[groupname]), f'The group {groupname} is not loaded'
        return ((index, self.poses[groupname][index], self.errors[index]) for index in self.errors)

    def constrain(self, groupname:str='relaxed'):
        if len(self.original_poses) == 0:
            raise ValueError('Load poses first.')
        for index, pose, error in self._generator_poses(groupname):
            add_pae_constraints(pose, error)

    def relax(self, cycles: int = 3):
        if self.original_poses is None:
            raise ValueError('Load poses first.')
        scorefxn = pyrosetta.get_fa_scorefxn()
        ap_st = pyrosetta.rosetta.core.scoring.ScoreType.atom_pair_constraint
        scorefxn.set_weight(ap_st, 1)
        for index, pose, error in self._generator_poses('original'):
            self.relaxed_poses[index] = pose.clone()
            relax = pyrosetta.rosetta.protocols.relax.FastRelax(scorefxn, cycles)
            relax.apply(self.relaxed_poses[index])
        # Add dG
        scorefxn.set_weight(ap_st, 0)
        self.scores['dG'] = self.scores['rank'].apply(lambda rank: scorefxn(self.relaxed_poses[rank]))

    def constrain_and_relax(self, cycles: int = 3):
        self.constrain()
        self.relax(cycles)

    def calculate_interface(self, interface='A_B'):
        self.get_median_interface_bfactors()
        # interface score.
        if 'dG' not in self.scores:
            return
        newdata = []
        for rank in self.scores['rank']:
            ia = pyrosetta.rosetta.protocols.analysis.InterfaceAnalyzerMover(interface)
            ia.apply(self.relaxed_poses[rank])
            newdata.append({'complex_energy':             ia.get_complex_energy(),
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

    def dump_pdbs(self,
                  groupname: str='relaxed',
                  prefix: Optional[str] = '',
                  folder: Optional[str] = None):
        for index, pose, error in self._generator_poses(groupname):
            path = f'{prefix}rank{index}.pdb'
            if folder:
                path = os.path.join(folder, f'{prefix}{groupname}_rank{index}.pdb')
            if not os.path.exists(folder):
                os.mkdir(folder)
            pose.dump_pdb(path)

    def dump(self, folder: str):
        if not os.path.exists(folder):
            os.mkdir(folder)
        self.scores.to_csv(os.path.join(folder, 'scores.csv'))
        self.dump_pdbs(folder=folder)
        with open(os.path.join(folder, 'errors.p', 'w')) as fh:
            pickle.dump(self.errors, fh)
        for groupname in self.poses:
            if len(self.poses[groupname]) != 0:
                self.dump_pdbs(groupname=groupname, folder=folder)

    @classmethod
    def load(cls, folder: str, params=()):
        self = cls(folder=folder, load_poses=False)
        self.folder = folder
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
        chain_sele = pr_rs.ChainSelector(chain_id)
        other_chains_sele = pr_rs.NotResidueSelector(chain_sele)
        cc_sele = pr_rs.CloseContactResidueSelector()
        cc_sele.central_residue_group_selector(other_chains_sele)
        cc_sele.threshold(float(threshold))
        other_cc_sele = pr_rs.AndResidueSelector(chain_sele, cc_sele)
        return pr_rs.ResidueVector(other_cc_sele.apply(pose))

    def find_interface_residues(self):
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

    def _get_median_interface_bfactor(self, pose, residues):
        pbd_info = pose.pdb_info()
        bfactors = [pbd_info.bfactor(r, pose.residue(r).atom_index('CA')) for r in residues]
        return np.median(bfactors)

    def get_median_interface_bfactors(self):
        self.find_interface_residues()
        for i, row in self.scores.iterrows():
            residues = list(row.interchain_residues_1) + list(row.interchain_residues_2)
            pose = self.relaxed_poses[row['rank']]  # ``row.rank`` is a function, just like ``row.name``.
            return self._get_median_interface_bfactor(pose, residues)