import ast
import os
import os.path as osp
import json
import pandas as pd
import numpy as np
import torch

from torch_geometric.data import InMemoryDataset, Data
from .data_utils import smiles2graph, scaffold_split

import torch
import numpy as np
import pandas as pd
from rdkit.Chem import AllChem
from rdkit.Chem.Descriptors import MolWt
from rdkit.Chem.Crippen import MolLogP
from rdkit.Chem.Lipinski import NumHAcceptors, NumHDonors
from rdkit import DataStructs
from rdkit.Chem.rdMolDescriptors import CalcExactMolWt, CalcTPSA
from rdkit.Chem.rdmolops import GetAdjacencyMatrix
from rdkit.Chem.Lipinski import *
from rdkit.Chem.AtomPairs import Torsions, Pairs
from rdkit.Chem import MACCSkeys 
from rdkit.Chem import rdMolDescriptors
from rdkit.Chem import ChemicalFeatures
from rdkit.Chem.Pharm2D.SigFactory import SigFactory
from rdkit.Chem.Pharm2D import Gobbi_Pharm2D, Generate
from rdkit.Chem import AllChem
from rdkit.Chem import MACCSkeys 

import numpy as np
from unimol_tools import UniMolRepr
def get_maccs_fingerprint(mol):
    """
    生成 MACCS Keys 指纹，返回长度 167 的 0/1 列表
    """
    fp = AllChem.GetMACCSKeysFingerprint(mol)
    return [int(b) for b in fp.ToBitString()]

def smiles_to_fingerprint(df, 
                         smiles_col="smiles",
                         morgan_radius=1,   # Morgan指纹半径
                         morgan_n_bits=1024, # Morgan指纹位数 
                         rdk_max_path=5,    # RDK最大路径长度
                         rdk_fp_size=1024,  # RDK指纹位数
                         include_rdk=False,  # 是否包含RDK指纹
                         include_maccs=True):  # 是否包含MACCS密钥
    
    # 提取SMILES列表
    smiles_list = df[smiles_col].tolist()
    
    # 转换SMILES为分子对象
    mols = [Chem.MolFromSmiles(smi) for smi in smiles_list]
    
    # 生成Morgan指纹
    morgan_fps = []
    for mol in mols:
        fp = AllChem.GetMorganFingerprintAsBitVect(mol, morgan_radius, nBits=morgan_n_bits)
        arr = np.zeros((morgan_n_bits,), dtype=np.int8)
        DataStructs.ConvertToNumpyArray(fp, arr)
        morgan_fps.append(arr)
    morgan_fps = np.array(morgan_fps, dtype=np.int8)
    
    # 生成RDK指纹（如果包含）
    rdk_fps = []
    if include_rdk:
        for mol in mols:
            fp = Chem.RDKFingerprint(mol, maxPath=rdk_max_path, fpSize=rdk_fp_size)
            arr = np.zeros((rdk_fp_size,), dtype=np.int8)
            DataStructs.ConvertToNumpyArray(fp, arr)
            rdk_fps.append(arr)
        rdk_fps = np.array(rdk_fps, dtype=np.int8)
    
    # 生成MACCS密钥（如果包含）
    maccs_fps = []
    if include_maccs:
        maccs_fps = np.array([get_maccs_fingerprint(mol) for mol in mols],
                             dtype=np.int8)
    
    # 合并特征
    combined = [morgan_fps]
    if include_rdk:
        combined.append(rdk_fps)
    if include_maccs:
        combined.append(maccs_fps)
    
    # 转换为张量
    combined = np.concatenate(combined, axis=1)
    return torch.tensor(combined)


class PygPredictionMoleculeDataset(InMemoryDataset):
    def __init__(
        self, name="chembl2k", root="raw_data", transform=None, pre_transform=None
    ):
        self.name = name
        self.root = osp.join(root, name)
        self.task_type = 'finetune'

        self.eval_metric = "roc_auc"
        if name == "chembl2k":
            self.num_tasks = 41
            self.start_column = 4
        elif name == "broad6k":
            self.num_tasks = 32
            self.start_column = 2
        elif name == "biogenadme":
            self.num_tasks = 6
            self.start_column = 4
            self.eval_metric = "avg_mae"
        # elif name == "moltoxcast":
        #     self.num_tasks = 617
        #     self.start_column = 2
        else:
            meta_path = osp.join(self.root, "raw", "meta.json")
            if os.path.exists(meta_path):
                with open(meta_path, "r") as f:
                    meta = json.load(f)
                self.num_tasks = meta["num_tasks"]
                self.start_column = meta["start_column"]
                self.eval_metric = meta["eval_metric"]
            else:
                raise ValueError("Invalid dataset name")

        super(PygPredictionMoleculeDataset, self).__init__(
            self.root, transform, pre_transform
        )
        self.data, self.slices = torch.load(self.processed_paths[0])

    def get_idx_split(self):
        path = osp.join(self.root, "split", "scaffold")

        if os.path.isfile(os.path.join(path, "split_dict.pt")):
            return torch.load(os.path.join(path, "split_dict.pt"))
        else:
            print("Initializing split...")
            data_df = pd.read_csv(osp.join(self.raw_dir, "assays.csv.gz"))
            train_idx, valid_idx, test_idx = scaffold_split(data_df)
            train_idx = torch.tensor(train_idx, dtype=torch.long)
            valid_idx = torch.tensor(valid_idx, dtype=torch.long)
            test_idx = torch.tensor(test_idx, dtype=torch.long)
            os.makedirs(path, exist_ok=True)
            torch.save(
                {"train": train_idx, "valid": valid_idx, "test": test_idx},
                os.path.join(path, "split_dict.pt"),
            )
        return {"train": train_idx, "valid": valid_idx, "test": test_idx}

    @property
    def raw_file_names(self):
        return ["assays.csv.gz"]

    @property
    def processed_file_names(self):
        return ["geometric_data_processed.pt"]

    def download(self):
        assert os.path.exists(
            os.path.join(self.raw_dir, "assays.csv.gz")
        ), f"assays.csv.gz does not exist in {self.raw_dir}"

    def process(self):
        data_df = pd.read_csv(osp.join(self.raw_dir, "assays.csv.gz"))
        mol_data = smiles_to_fingerprint(data_df)
        clf = UniMolRepr(data_type='molecule', remove_hs=False)
        #clf = UniMolRepr(data_type='molecule', remove_hs=False, model_name = 'unimolv2', model_size = '84m') #84m, 164m, 310m, 570m, 1.1B.
        unimol_repr = clf.get_repr(data_df["smiles"].tolist(), return_atomic_reprs=True)
        # CLS token repr
        unimol_feat = torch.tensor(unimol_repr['cls_repr'])

        mol_rf_path = os.path.join(self.raw_dir, 'rf_pred.npy')
        rf_pred = np.load(mol_rf_path)
        rf_pred = torch.tensor(rf_pred)

        pyg_graph_list = []
        for idx, row in data_df.iterrows():
            smiles = row["smiles"]
            graph = smiles2graph(smiles)

            g = Data()
            g.num_nodes = graph["num_nodes"]
            g.edge_index = torch.from_numpy(graph["edge_index"])

            del graph["num_nodes"]
            del graph["edge_index"]

            if graph["edge_feat"] is not None:
                g.edge_attr = torch.from_numpy(graph["edge_feat"])
                del graph["edge_feat"]

            if graph["node_feat"] is not None:
                g.x = torch.from_numpy(graph["node_feat"])
                del graph["node_feat"]

            try:
                g.fp = torch.tensor(graph["fp"], dtype=torch.int8).view(1, -1)
                del graph["fp"]
            except:
                pass
            g.mol_features = mol_data[idx].unsqueeze(0)
            g.unimol_features  = unimol_feat[idx].unsqueeze(0)
            g.rf_pred  = rf_pred[idx].unsqueeze(0)

            y = []
            for col in range(self.start_column, len(row)):
                y.append(float(row.iloc[col]))

            g.y = torch.tensor(y, dtype=torch.float32).view(1, -1)
            pyg_graph_list.append(g)

        pyg_graph_list = (
            pyg_graph_list
            if self.pre_transform is None
            else self.pre_transform(pyg_graph_list)
        )
        print("Saving...")
        torch.save(self.collate(pyg_graph_list), self.processed_paths[0])

    def __repr__(self):
        return "{}()".format(self.__class__.__name__)


class PredictionMoleculeDataset(object):
    def __init__(self, name="chembl2k", root="raw_data", transform="smiles"):

        assert transform in [
            "smiles",
            "fingerprint",
            "morphology",
            "expression",
        ], "Invalid transform type"
        
        self.name = name
        self.folder = osp.join(root, name)
        self.transform = transform
        self.raw_data = os.path.join(self.folder, "raw", "assays.csv.gz")

        self.eval_metric = "roc_auc"
        if name == "chembl2k":
            self.num_tasks = 41
            self.start_column = 4
        elif name == "broad6k":
            self.num_tasks = 32
            self.start_column = 2
        elif "moltoxcast" in self.name:
            self.num_tasks = 617
            self.start_column = 2
        elif name == "biogenadme":
            self.num_tasks = 6
            self.start_column = 4
            self.eval_metric = "avg_mae"
        else:
            meta_path = osp.join(self.folder, "raw", "meta.json")
            if os.path.exists(meta_path):
                with open(meta_path, "r") as f:
                    meta = json.load(f)
                self.num_tasks = meta["num_tasks"]
                self.start_column = meta["start_column"]
            else:
                raise ValueError("Invalid dataset name")

        super(PredictionMoleculeDataset, self).__init__()
        if transform == "smiles":
            self.prepare_smiles()
        elif transform == "fingerprint":
            self.prepare_fingerprints()
        elif transform in ["morphology", "expression"]:
            self.prepare_other_modality()

    def get_idx_split(self, to_list=False):
        path = osp.join(self.folder, "split", "scaffold")
        if os.path.isfile(os.path.join(path, "split_dict.pt")):
            split_dict = torch.load(os.path.join(path, "split_dict.pt"))
        else:
            data_df = pd.read_csv(self.raw_data)
            train_idx, valid_idx, test_idx = scaffold_split(data_df)
            train_idx = torch.tensor(train_idx, dtype=torch.long)
            valid_idx = torch.tensor(valid_idx, dtype=torch.long)
            test_idx = torch.tensor(test_idx, dtype=torch.long)

            os.makedirs(path, exist_ok=True)
            torch.save(
                {"train": train_idx, "valid": valid_idx, "test": test_idx},
                os.path.join(path, "split_dict.pt"),
            )
            split_dict = {"train": train_idx, "valid": valid_idx, "test": test_idx}

        if to_list:
            split_dict = {k: v.tolist() for k, v in split_dict.items()}
        return split_dict

    def prepare_other_modality(self):
        assert os.path.exists(
            self.raw_data
        ), f" {self.raw_data} assays.csv.gz does not exist"
        data_df = pd.read_csv(self.raw_data)

        processed_dir = osp.join(self.folder, "processed")
        os.makedirs(processed_dir, exist_ok=True)

        if self.transform == "morphology":
            if self.name == "chembl2k":
                feature_df = pd.read_csv(
                    os.path.join(self.folder, "raw", "CP-JUMP.csv.gz"),
                    compression="gzip",
                )
                feature_arr = np.load(
                    os.path.join(self.folder, "raw", "CP-JUMP_feature.npz")
                )["data"]
            else:
                feature_df = pd.read_csv(
                    os.path.join(self.folder, "raw", "CP-Bray.csv.gz"),
                    compression="gzip",
                )
                feature_arr = np.load(
                    os.path.join(self.folder, "raw", "CP-Bray_feature.npz")
                )["data"]
        else:
            feature_df = pd.read_csv(
                os.path.join(self.folder, "raw", "GE.csv.gz"), compression="gzip"
            )
            feature_arr = np.load(os.path.join(self.folder, "raw", "GE_feature.npz"))[
                "data"
            ]

        if not osp.exists(osp.join(processed_dir, f"processed_{self.transform}.pt")):
            x_list = []
            y_list = []
            feature_dim = feature_arr.shape[1]
            for idx, row in data_df.iterrows():
                if len(feature_df[feature_df["inchikey"] == row["inchikey"]]) == 0:
                    x_list.append(torch.tensor([float("nan")] * feature_dim))
                else:
                    x_tensor = torch.tensor(
                        feature_arr[
                            feature_df[
                                feature_df["inchikey"] == row["inchikey"]
                            ].index.tolist()[0]
                        ],
                        dtype=torch.float32,
                    )
                    x_list.append(x_tensor)

                y = []
                for col in range(self.start_column, len(row)):
                    y.append(float(row.iloc[col]))
                y = torch.tensor(y, dtype=torch.float32)
                y_list.append(y)

            x_list = torch.stack(x_list, dim=0)
            y_list = torch.stack(y_list, dim=0)
            torch.save(
                (x_list, y_list),
                osp.join(processed_dir, f"processed_{self.transform}.pt"),
            )
        else:
            x_list, y_list = torch.load(
                osp.join(processed_dir, f"processed_{self.transform}.pt")
            )

        self.data = x_list
        self.labels = y_list

    def prepare_smiles(self):
        assert os.path.exists(
            self.raw_data
        ), f" {self.raw_data} assays.csv.gz does not exist"
        data_df = pd.read_csv(self.raw_data)

        processed_dir = osp.join(self.folder, "processed")
        os.makedirs(processed_dir, exist_ok=True)
        x_list = []
        y_list = []
        for idx, row in data_df.iterrows():
            smiles = row["smiles"]
            x_list.append(smiles)
            y = []
            for col in range(self.start_column, len(row)):
                y.append(float(row.iloc[col]))
            y = torch.tensor(y, dtype=torch.float32)
            y_list.append(y)

        self.data = x_list
        self.labels = y_list

    def prepare_fingerprints(self):
        assert os.path.exists(
            self.raw_data
        ), f" {self.raw_data} assays.csv.gz does not exist"
        data_df = pd.read_csv(self.raw_data)

        processed_dir = osp.join(self.folder, "processed")
        os.makedirs(processed_dir, exist_ok=True)

        if not osp.exists(osp.join(processed_dir, "processed_fp.pt")):
            print("Processing fingerprints...")
            from rdkit import Chem
            from rdkit.Chem import AllChem

            x_list = []
            y_list = []
            for idx, row in data_df.iterrows():
                smiles = row["smiles"]
                mol = Chem.MolFromSmiles(smiles)
                x = torch.tensor(
                    list(AllChem.GetMorganFingerprintAsBitVect(mol, 2)),
                    dtype=torch.float32,
                )
                x_list.append(x)
                y = []
                for col in range(self.start_column, len(row)):
                    y.append(float(row.iloc[col]))
                y = torch.tensor(y, dtype=torch.float32)
                y_list.append(y)

            x_list = torch.stack(x_list, dim=0)
            y_list = torch.stack(y_list, dim=0)
            torch.save((x_list, y_list), osp.join(processed_dir, "processed_fp.pt"))
        else:
            x_list, y_list = torch.load(osp.join(processed_dir, "processed_fp.pt"))

        self.data = x_list
        self.labels = y_list

    def __getitem__(self, idx):
        """Get datapoint(s) with index(indices)"""

        if isinstance(idx, (int, np.integer)):
            return self.data[idx], self.labels[idx]
        elif isinstance(idx, (list, np.ndarray)):
            return [self.data[i] for i in idx], [self.labels[i] for i in idx]
        elif isinstance(idx, torch.LongTensor):
            return self.data[idx], self.labels[idx]

        raise IndexError("Not supported index {}.".format(type(idx).__name__))

    def __len__(self):
        return len(self.data)

    def __repr__(self):
        return "{}({})".format(self.__class__.__name__, len(self))


if __name__ == "__main__":
    pass
