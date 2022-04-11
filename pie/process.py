import os
from abc import ABC, abstractmethod
from typing import Any, Optional, Tuple

import numpy as np

from pie import np_solver

CPU_COUNT = os.cpu_count() or 1
DEFAULT_BACKEND = "numpy"
ALL_BACKEND = ["numpy"]

try:
  from pie import taichi_solver
  ALL_BACKEND += ["taichi-cpu", "taichi-gpu", "taichi-cuda"]
  DEFAULT_BACKEND = "taichi-cpu"
except ImportError:
  taichi_solver = None  # type: ignore

try:
  from pie import pie_core_gcc  # type: ignore
  DEFAULT_BACKEND = "gcc"
  ALL_BACKEND.append("gcc")
except ImportError:
  pie_core_gcc = None

try:
  from pie import pie_core_openmp  # type: ignore
  DEFAULT_BACKEND = "openmp"
  ALL_BACKEND.append("openmp")
except ImportError:
  pie_core_openmp = None

try:
  from mpi4py import MPI
except ImportError:
  MPI = None  # type: ignore

try:
  from pie import pie_core_mpi  # type: ignore
  ALL_BACKEND.append("mpi")
except ImportError:
  pie_core_mpi = None

try:
  from pie import pie_core_cuda  # type: ignore
  ALL_BACKEND.append("cuda")
except ImportError:
  pie_core_cuda = None


class BaseProcessor(ABC):
  """API definition for processor class."""

  def __init__(
    self, gradient: str, rank: int, backend: str, core: Optional[Any]
  ):
    if core is None:
      error_msg = {
        "numpy":
          "Please run `pip install numpy`.",
        "gcc":
          "Please install cmake and gcc in your operating system.",
        "openmp":
          "Please make sure your gcc is compatible with `-fopenmp` option.",
        "mpi":
          "Please install MPI and run `pip install mpi4py`.",
        "cuda":
          "Please make sure nvcc and cuda-related libraries are available.",
        "taichi":
          "Please run `pip install taichi`.",
      }
      print(error_msg[backend.split("-")[0]])

      raise AssertionError(f"Invalid backend {backend}.")

    self.gradient = gradient
    self.rank = rank
    self.backend = backend
    self.core = core
    self.root = rank == 0

  def mixgrad(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if self.gradient == "src":
      return a
    if self.gradient == "avg":
      return (a + b) / 2
    # mix gradient, see Equ. 12 in PIE paper
    mask = np.abs(a) < np.abs(b)
    # mask = (a ** 2).sum(-1) < (b ** 2).sum(-1)
    a[mask] = b[mask]
    return a

  @abstractmethod
  def reset(
    self,
    src: np.ndarray,
    mask: np.ndarray,
    tgt: np.ndarray,
    mask_on_src: Tuple[int, int],
    mask_on_tgt: Tuple[int, int],
  ) -> int:
    pass

  def sync(self) -> None:
    self.core.sync()

  def step(self, iteration: int) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    pass


class EquProcessor(BaseProcessor):
  """PIE Jacobi equation processor."""

  def __init__(
    self,
    gradient: str = "max",
    backend: str = DEFAULT_BACKEND,
    n_cpu: int = CPU_COUNT,
    min_interval: int = 100,
    block_size: int = 1024,
  ):
    core: Optional[Any] = None
    rank = 0

    if backend == "numpy":
      core = np_solver.EquSolver()
    elif backend == "gcc":
      core = pie_core_gcc.EquSolver()
    elif backend == "openmp" and pie_core_openmp is not None:
      core = pie_core_openmp.EquSolver(n_cpu)
    elif backend == "mpi" and pie_core_mpi is not None:
      core = pie_core_mpi.EquSolver(min_interval)
      rank = MPI.COMM_WORLD.Get_rank()
    elif backend == "cuda" and pie_core_cuda is not None:
      core = pie_core_cuda.EquSolver(block_size)
    elif backend.startswith("taichi") and taichi_solver is not None:
      core = taichi_solver.EquSolver(backend, n_cpu, block_size)

    super().__init__(gradient, rank, backend, core)

  def mask2index(
    self, mask: np.ndarray
  ) -> Tuple[np.ndarray, int, np.ndarray, np.ndarray]:
    x, y = np.nonzero(mask)
    max_id = x.shape[0] + 1
    index = np.zeros((max_id, 3))
    ids = self.core.partition(mask)
    ids[mask == 0] = 0  # reserve id=0 for constant
    index = ids[x, y].argsort()
    return ids, max_id, x[index], y[index]

  def reset(
    self,
    src: np.ndarray,
    mask: np.ndarray,
    tgt: np.ndarray,
    mask_on_src: Tuple[int, int],
    mask_on_tgt: Tuple[int, int],
  ) -> int:
    assert self.root
    # check validity
    # assert 0 <= mask_on_src[0] and 0 <= mask_on_src[1]
    # assert mask_on_src[0] + mask.shape[0] <= src.shape[0]
    # assert mask_on_src[1] + mask.shape[1] <= src.shape[1]
    # assert mask_on_tgt[0] + mask.shape[0] <= tgt.shape[0]
    # assert mask_on_tgt[1] + mask.shape[1] <= tgt.shape[1]

    if len(mask.shape) == 3:
      mask = mask.mean(-1)
    mask = (mask >= 128).astype(np.int32)

    # zero-out edge
    mask[0] = 0
    mask[-1] = 0
    mask[:, 0] = 0
    mask[:, -1] = 0

    x, y = np.nonzero(mask)
    x0, x1 = x.min() - 1, x.max() + 2
    y0, y1 = y.min() - 1, y.max() + 2
    mask_on_src = (x0 + mask_on_src[0], y0 + mask_on_src[1])
    mask_on_tgt = (x0 + mask_on_tgt[0], y0 + mask_on_tgt[1])
    mask = mask[x0:x1, y0:y1]
    ids, max_id, index_x, index_y = self.mask2index(mask)

    src_x, src_y = index_x + mask_on_src[0], index_y + mask_on_src[1]
    tgt_x, tgt_y = index_x + mask_on_tgt[0], index_y + mask_on_tgt[1]

    src_C = src[src_x, src_y].astype(np.float32)
    src_U = src[src_x - 1, src_y].astype(np.float32)
    src_D = src[src_x + 1, src_y].astype(np.float32)
    src_L = src[src_x, src_y - 1].astype(np.float32)
    src_R = src[src_x, src_y + 1].astype(np.float32)
    tgt_C = tgt[tgt_x, tgt_y].astype(np.float32)
    tgt_U = tgt[tgt_x - 1, tgt_y].astype(np.float32)
    tgt_D = tgt[tgt_x + 1, tgt_y].astype(np.float32)
    tgt_L = tgt[tgt_x, tgt_y - 1].astype(np.float32)
    tgt_R = tgt[tgt_x, tgt_y + 1].astype(np.float32)

    grad = self.mixgrad(src_C - src_L, tgt_C - tgt_L) \
      + self.mixgrad(src_C - src_R, tgt_C - tgt_R) \
      + self.mixgrad(src_C - src_U, tgt_C - tgt_U) \
      + self.mixgrad(src_C - src_D, tgt_C - tgt_D)

    A = np.zeros((max_id, 4), np.int32)
    X = np.zeros((max_id, 3), np.float32)
    B = np.zeros((max_id, 3), np.float32)

    X[1:] = tgt[index_x + mask_on_tgt[0], index_y + mask_on_tgt[1]]
    # four-way
    A[1:, 0] = ids[index_x - 1, index_y]
    A[1:, 1] = ids[index_x + 1, index_y]
    A[1:, 2] = ids[index_x, index_y - 1]
    A[1:, 3] = ids[index_x, index_y + 1]
    B[1:] = grad
    m = (mask[index_x - 1, index_y] == 0).astype(float).reshape(-1, 1)
    B[1:] += m * tgt[index_x + mask_on_tgt[0] - 1, index_y + mask_on_tgt[1]]
    m = (mask[index_x, index_y - 1] == 0).astype(float).reshape(-1, 1)
    B[1:] += m * tgt[index_x + mask_on_tgt[0], index_y + mask_on_tgt[1] - 1]
    m = (mask[index_x, index_y + 1] == 0).astype(float).reshape(-1, 1)
    B[1:] += m * tgt[index_x + mask_on_tgt[0], index_y + mask_on_tgt[1] + 1]
    m = (mask[index_x + 1, index_y] == 0).astype(float).reshape(-1, 1)
    B[1:] += m * tgt[index_x + mask_on_tgt[0] + 1, index_y + mask_on_tgt[1]]

    self.tgt = tgt.copy()
    self.tgt_index = (index_x + mask_on_tgt[0], index_y + mask_on_tgt[1])
    self.core.reset(max_id, A, X, B)
    return max_id

  def step(self, iteration: int) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    result = self.core.step(iteration)
    if self.root:
      x, err = result
      self.tgt[self.tgt_index] = x[1:]
      return self.tgt, err
    return None


class GridProcessor(BaseProcessor):
  """PIE grid processor."""

  def __init__(
    self,
    gradient: str = "mix",
    backend: str = DEFAULT_BACKEND,
    n_cpu: int = CPU_COUNT,
    min_interval: int = 100,
    block_size: int = 1024,
    grid_x: int = 8,
    grid_y: int = 8,
  ):
    core: Optional[Any] = None
    rank = 0

    if backend == "numpy":
      core = np_solver.GridSolver()
    elif backend == "gcc":
      core = pie_core_gcc.GridSolver(grid_x, grid_y)
    elif backend == "openmp" and pie_core_openmp is not None:
      core = pie_core_openmp.GridSolver(grid_x, grid_y, n_cpu)
    elif backend == "mpi" and pie_core_mpi is not None:
      core = pie_core_mpi.GridSolver(min_interval)
      rank = MPI.COMM_WORLD.Get_rank()
    elif backend == "cuda" and pie_core_cuda is not None:
      core = pie_core_cuda.GridSolver(grid_x, grid_y)
    elif backend.startswith("taichi") and taichi_solver is not None:
      core = taichi_solver.GridSolver(
        grid_x, grid_y, backend, n_cpu, block_size
      )

    super().__init__(gradient, rank, backend, core)

  def reset(
    self,
    src: np.ndarray,
    mask: np.ndarray,
    tgt: np.ndarray,
    mask_on_src: Tuple[int, int],
    mask_on_tgt: Tuple[int, int],
  ) -> int:
    assert self.root
    # check validity
    # assert 0 <= mask_on_src[0] and 0 <= mask_on_src[1]
    # assert mask_on_src[0] + mask.shape[0] <= src.shape[0]
    # assert mask_on_src[1] + mask.shape[1] <= src.shape[1]
    # assert mask_on_tgt[0] + mask.shape[0] <= tgt.shape[0]
    # assert mask_on_tgt[1] + mask.shape[1] <= tgt.shape[1]

    if len(mask.shape) == 3:
      mask = mask.mean(-1)
    mask = (mask >= 128).astype(np.int32)

    # zero-out edge
    mask[0] = 0
    mask[-1] = 0
    mask[:, 0] = 0
    mask[:, -1] = 0

    x, y = np.nonzero(mask)
    x0, x1 = x.min() - 1, x.max() + 2
    y0, y1 = y.min() - 1, y.max() + 2
    mask = mask[x0:x1, y0:y1]
    max_id = np.prod(mask.shape)

    src_crop = src[mask_on_src[0] + x0:mask_on_src[0] + x1,
                   mask_on_src[1] + y0:mask_on_src[1] + y1].astype(np.float32)
    tgt_crop = tgt[mask_on_tgt[0] + x0:mask_on_tgt[0] + x1,
                   mask_on_tgt[1] + y0:mask_on_tgt[1] + y1].astype(np.float32)
    grad = np.zeros([*mask.shape, 3], np.float32)
    grad[1:] += self.mixgrad(
      src_crop[1:] - src_crop[:-1], tgt_crop[1:] - tgt_crop[:-1]
    )
    grad[:-1] += self.mixgrad(
      src_crop[:-1] - src_crop[1:], tgt_crop[:-1] - tgt_crop[1:]
    )
    grad[:, 1:] += self.mixgrad(
      src_crop[:, 1:] - src_crop[:, :-1], tgt_crop[:, 1:] - tgt_crop[:, :-1]
    )
    grad[:, :-1] += self.mixgrad(
      src_crop[:, :-1] - src_crop[:, 1:], tgt_crop[:, :-1] - tgt_crop[:, 1:]
    )

    grad[mask == 0] = 0

    self.x0 = mask_on_tgt[0] + x0
    self.x1 = mask_on_tgt[0] + x1
    self.y0 = mask_on_tgt[1] + y0
    self.y1 = mask_on_tgt[1] + y1
    self.tgt = tgt.copy()
    self.core.reset(max_id, mask, tgt_crop, grad)
    return max_id

  def step(self, iteration: int) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    result = self.core.step(iteration)
    if self.root:
      tgt, err = result
      self.tgt[self.x0:self.x1, self.y0:self.y1] = tgt
      return self.tgt, err
    return None
