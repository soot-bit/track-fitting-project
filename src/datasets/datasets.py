from typing import Optional, Union, List
from tqdm import tqdm
from torch.utils.data import TensorDataset, Subset, random_split, DataLoader, Dataset, IterableDataset
import torch
from src.datasets.utils import ParticleGun, Detector, EventGenerator
from rich import print
import numpy as np
from torch.nn.utils.rnn import pad_sequence
import lightning as L
from rainbow_print import printr
import os
import shutil
import math
from trackml.dataset import load_dataset, load_event
import pandas as pd





            #################################
            #   Track Datasets with padding #
            #################################

class TracksDataset(IterableDataset):
    """
        Generates trackdata on the fly
    see https://github.com/ryanliu30
    """
    def __init__(
            self,
            hole_inefficiency: Optional[float] = 0,
            d0: Optional[float] = 0.1,
            noise: Optional[Union[float, List[float], List[Union[float, str]]]] = 0,
            lambda_: Optional[float] = 50,
            pt_dist: Optional[Union[float, List[float], List[Union[float, str]]]] = [1, 5],
            warmup_t0: Optional[float] = 0,
        ):
        super().__init__()

        self.hole_inefficiency = hole_inefficiency
        self.d0 = d0
        self.noise = noise
        self.lambda_ = lambda_
        self.pt_dist = pt_dist

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        return _TrackIterable(
            self.hole_inefficiency,
            self.d0,
            self.noise,
            self.lambda_,
            self.pt_dist,
        )

class _TrackIterable:
    def __init__(
            self,
            hole_inefficiency: Optional[float] = 0,
            d0: Optional[float] = 0.1,
            noise: Optional[Union[float, List[float], List[Union[float, str]]]] = 0,
            lambda_: Optional[float] = 50,
            pt_dist: Optional[Union[float, List[float], List[Union[float, str]]]] = [1, 10],
            warmup_t0: Optional[float] = 0
        ):

        self.detector = Detector(
            dimension=2,
            hole_inefficiency=hole_inefficiency
        ).add_from_template(
            'barrel',
            min_radius=0.5,
            max_radius=3,
            number_of_layers=10,
        )

        self.particle_gun = ParticleGun(
            dimension=2,
            num_particles=1,
            pt=pt_dist,
            pphi=[-np.pi, np.pi],
            vx=[0, d0 * 0.5**0.5, 'normal'],
            vy=[0, d0 * 0.5**0.5, 'normal'],
        )

        self.event_gen = EventGenerator(self.particle_gun, self.detector, noise)

    def __next__(self):
        event = self.event_gen.generate_event()

        pt = event.particles.pt

        x = torch.tensor([event.hits.x, event.hits.y], dtype=torch.float).T.contiguous()
        mask = torch.ones(x.shape[0], dtype=bool)

        return x, mask, torch.tensor([pt], dtype=torch.float), event

class TracksDatasetWrapper(Dataset):
    """ Generates and stores track data  
        ---------------------------------
    """ 
    def __init__(self, num_events: int = 200):
        self.tracks_dataset = TracksDataset()
        self.num_events = num_events
        self.events = []

        self._generate_events()

    def _generate_events(self):
        iterable = iter(self.tracks_dataset)
        for _ in range(self.num_events):
            self.events.append(next(iterable))

    def __len__(self):
        return self.num_events

    def __getitem__(self, idx):
        return self.events[idx]

           
           
           
            ###################################
            # ToyTrack Lightning Data Module  #
            ###################################
class ToyTrackDataModule(L.LightningDataModule):
    def __init__(
        self,
        use_tracks_dataset: bool = True,
        batch_size: int = 20,
        wrapper_size:int = 200,
        num_workers: int = 10,
        persistence: bool = False

    ):
        super().__init__()
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.persistence = persistence if num_workers == 0 else True

        if use_tracks_dataset:
            self.dataset = TracksDataset()
        else:
            self.dataset = TracksDatasetWrapper(wrapper_size)


    def setup(self, stage=None):
        if isinstance(self.dataset, TracksDatasetWrapper):
            printr(f"**Using TracksDatasetWrapper size:: {len(self.dataset)}**")
            train_len = int(len(self.dataset) * 0.6)
            val_len = int(len(self.dataset) * 0.2)
            test_len = len(self.dataset) - train_len - val_len
            self.train_dataset, self.val_dataset, self.test_dataset = random_split(
                self.dataset, [train_len, val_len, test_len]
            )
        elif isinstance(self.dataset, TracksDataset):
            printr("**Using infinite TracksDataset***")
            self.train_dataset = self.dataset
            self.val_dataset = self.dataset
            self.test_dataset = self.dataset
        else:
            printr("Unknown dataset type:", type(self.dataset))

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            collate_fn=self.collate_fn,
            persistent_workers=self.persistence
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            collate_fn=self.collate_fn,
            persistent_workers=self.persistence
        )

    def test_dataloader(self):
        return DataLoader(
            self.test_dataset,
            batch_size=self.batch_size,
            collate_fn=self.collate_fn,
            num_workers=self.num_workers,
            persistent_workers=self.persistence
        )

    @staticmethod
    def collate_fn(ls):
        """Batch maker"""
        x, mask, pt, events = zip(*ls)
        return pad_sequence(x, batch_first=True), pad_sequence(mask, batch_first=True), torch.cat(pt).squeeze(), list(events)
    
    


class TrackMLIterableDataset2(IterableDataset):
    def __init__(self, data_path, tolerance=0.01):
        self.data_path = data_path
        self.tolerance = tolerance
        self.start, self.end = self._event_range()

    def _event_range(self):
        files = os.listdir(self.data_path)
        files.sort()
        if files:
            return int(files[0].split('-')[0][5:]), int(files[-1].split('-')[0][5:]) + 1
        else:
            raise FileNotFoundError("Uh-oh!, looks like the files are on vacation...")

    def _conformal_mapping(self, x, y, z):
        r = x**2 + y**2
        u = x / r
        v = y / r
        pp, vv = np.polyfit(u, v, 2, cov=True)
        b = 0.5 / pp[2]
        a = -pp[1] * b
        R = np.sqrt(a**2 + b**2)

        magnetic_field = 2.0
        pT = 0.3 * magnetic_field * R  # in MeV

        return pT / 1_000

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:  # single-process data loading,
            iter_start = self.start
            iter_end = self.end
        else:
            # split workload
            per_worker = int(math.ceil((self.end - self.start) / float(worker_info.num_workers)))
            worker_id = worker_info.id
            iter_start = self.start + worker_id * per_worker
            iter_end = min(iter_start + per_worker, self.end)

        for i in range(iter_start, iter_end):
            event = f'event00000{i:02d}'

            path = os.path.join(self.data_path, event)

            hits, cells, particles, truth = load_event(path)
            particles = particles[particles['nhits'] >= 5]
            merged_df = pd.merge(truth, particles, on='particle_id')
            merged_df = pd.merge(merged_df, hits, on='hit_id')

            merged_df['pT'] = np.sqrt(merged_df['px']**2 + merged_df['py']**2)

            grouped = merged_df.groupby('particle_id')

            for particle_id, group in grouped:
                inputs = group[['tx', 'ty', 'tz']].values
                target = group[['pT', 'pz']].values[0]

                zxy = torch.tensor(inputs, dtype=torch.float32)
                target_tensor = torch.tensor(target, dtype=torch.float32)

                x, y, z = zxy[:, 0], zxy[:, 1], zxy[:, 2]
                conf_pt = self._conformal_mapping(x, y, z)


                error = abs(conf_pt - target_tensor[0].item())
                if error < self.tolerance:
                    mask = torch.ones(zxy.shape[0], dtype=torch.bool)
                    yield (zxy, mask, target_tensor)


class TrackMLDataModule(L.LightningDataModule):
    def __init__(
        self,
        batch_size: int = 20,
        num_workers: int = 0,
        persistence: bool = False,
        data_path: str = "/content/track-fitter/src/datasets",
        ram_path: str = "/dev/shm/MyData",
        use_ram = False

    ):
        super().__init__()
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.persistence = persistence if num_workers == 0 else True
        self.data_path = data_path
        self.ram_path = ram_path
        self.use_ram = use_ram
        
        
        self._to_ram() if  self.use_ram else None


    def _to_ram(self):
        try:
            shutil.copytree(self.data_path, self.ram_path)
            print("Data copied successfully to RAM...")
        except Exception as e:
            if "File exists:" in str(e):
                pass
            else:
                raise RuntimeError (f"Ohh my: {e}")



    def setup(self, stage=None):
        printr("***Using TrackML Dataset****")
        data_path = self.ram_path if self.use_ram else self.data_path
        self.train_dataset = TrackMLIterableDataset2(os.path.join(data_path, "train"))
        self.val_dataset = TrackMLIterableDataset2(os.path.join(data_path, "val"))
        self.test_dataset = TrackMLIterableDataset2(os.path.join(data_path, "test"))

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            collate_fn=self.TMLcollate_fn,
            persistent_workers=self.persistence
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            collate_fn=self.TMLcollate_fn,
            persistent_workers=self.persistence
        )

    def test_dataloader(self):
        return DataLoader(
            self.test_dataset,
            batch_size=self.batch_size,
            collate_fn=self.TMLcollate_fn,
            num_workers=self.num_workers,
            persistent_workers=self.persistence
        )



    @staticmethod
    def TMLcollate_fn(batch):
        inputs, masks, targets = zip(*batch)

        inputs = pad_sequence(inputs, batch_first=True)
        masks = pad_sequence(masks, batch_first=True, padding_value=0)

        return inputs, masks, torch.stack(targets, dim=0), None




