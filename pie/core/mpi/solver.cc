#include "solver.h"

#include <mpi.h>

class MPIEquSolver : public EquSolver {
  int* buf;
  unsigned char* buf2;
  float* tmp;
  int proc_id, n_proc, min_interval, *offset;

 public:
  explicit MPIEquSolver(int min_interval)
      : buf(NULL),
        buf2(NULL),
        tmp(NULL),
        min_interval(min_interval),
        EquSolver() {
    MPI_Comm_rank(MPI_COMM_WORLD, &proc_id);
    MPI_Comm_size(MPI_COMM_WORLD, &n_proc);
    offset = new int[n_proc + 1];
  }

  ~MPIEquSolver() {
    if (buf != NULL) {
      delete[] buf, buf2;
    }
    if (tmp != NULL) {
      delete[] tmp;
    }
    delete[] offset;
  }

  py::array_t<int> partition(py::array_t<int> mask) {
    auto arr = mask.unchecked<2>();
    int n = arr.shape(0), m = arr.shape(1);
    if (buf != NULL) {
      delete[] buf;
    }
    buf = new int[n * m];
    int cnt = 0;
    for (int i = 0; i < n; ++i) {
      for (int j = 0; j < m; ++j) {
        if (arr(i, j) > 0) {
          buf[i * m + j] = ++cnt;
        } else {
          buf[i * m + j] = 0;
        }
      }
    }
    return py::array({n, m}, buf);
  }

  void post_reset() {
    if (tmp != NULL) {
      delete[] tmp, buf2;
    }
    tmp = new float[N * 3];
    buf2 = new unsigned char[N * 3];
    // offset
    offset[0] = 0;
    int additional = N % n_proc;
    for (int i = 0; i < n_proc; ++i) {
      offset[i + 1] = offset[i] + N / n_proc + (i < additional);
    }
  }

  void sync() {
    MPI_Bcast(&N, 1, MPI_INT, 0, MPI_COMM_WORLD);
    if (proc_id > 0) {
      if (A != NULL) {
        delete[] A, B, X, tmp;
      }
      A = new int[N * 4];
      B = new float[N * 3];
      X = new float[N * 3];
      tmp = new float[N * 3];
      buf2 = new unsigned char[N * 3];
    }
    MPI_Bcast(A, N * 4, MPI_INT, 0, MPI_COMM_WORLD);
    MPI_Bcast(B, N * 3, MPI_FLOAT, 0, MPI_COMM_WORLD);
    MPI_Bcast(X, N * 3, MPI_FLOAT, 0, MPI_COMM_WORLD);
    MPI_Bcast(offset, n_proc + 1, MPI_INT, 0, MPI_COMM_WORLD);
  }

  inline void update_equation(int i) {
    int off3 = i * 3;
    int off4 = i * 4;
    int id0 = A[off4 + 0] * 3;
    int id1 = A[off4 + 1] * 3;
    int id2 = A[off4 + 2] * 3;
    int id3 = A[off4 + 3] * 3;
    X[off3 + 0] =
        (B[off3 + 0] + X[id0 + 0] + X[id1 + 0] + X[id2 + 0] + X[id3 + 0]) / 4;
    X[off3 + 1] =
        (B[off3 + 1] + X[id0 + 1] + X[id1 + 1] + X[id2 + 1] + X[id3 + 1]) / 4;
    X[off3 + 2] =
        (B[off3 + 2] + X[id0 + 2] + X[id1 + 2] + X[id2 + 2] + X[id3 + 2]) / 4;
  }

  void calc_error() {
    for (int i = 1; i < N; ++i) {
      int off3 = i * 3;
      int off4 = i * 4;
      int id0 = A[off4 + 0] * 3;
      int id1 = A[off4 + 1] * 3;
      int id2 = A[off4 + 2] * 3;
      int id3 = A[off4 + 3] * 3;
      tmp[off3 + 0] = std::abs(
          4 * X[off3 + 0] -
          (X[id0 + 0] + X[id1 + 0] + X[id2 + 0] + X[id3 + 0]) - B[off3 + 0]);
      tmp[off3 + 1] = std::abs(
          4 * X[off3 + 1] -
          (X[id0 + 1] + X[id1 + 1] + X[id2 + 1] + X[id3 + 1]) - B[off3 + 1]);
      tmp[off3 + 2] = std::abs(
          4 * X[off3 + 2] -
          (X[id0 + 2] + X[id1 + 2] + X[id2 + 2] + X[id3 + 2]) - B[off3 + 2]);
    }
    memset(err, 0, sizeof(err));
    for (int i = 1; i < N; ++i) {
      int off3 = i * 3;
      err[0] += tmp[off3 + 0];
      err[1] += tmp[off3 + 1];
      err[2] += tmp[off3 + 2];
    }
  }

  std::tuple<py::array_t<unsigned char>, py::array_t<float>> step(
      int iteration) {
    for (int i = 0; i < iteration; i += min_interval) {
      for (int j = 0; j < min_interval; ++j) {
        for (int k = offset[proc_id]; k < offset[proc_id + 1]; ++k) {
          update_equation(k);
        }
      }
      if (proc_id == 0) {
        for (int j = 1; j < n_proc; ++j) {
          MPI_Recv(&X[offset[j] * 3], (offset[j + 1] - offset[j]) * 3,
                   MPI_FLOAT, j, 0, MPI_COMM_WORLD, NULL);
        }
      } else {
        MPI_Send(&X[offset[proc_id] * 3],
                 (offset[proc_id + 1] - offset[proc_id]) * 3, MPI_FLOAT, 0, 0,
                 MPI_COMM_WORLD);
      }
      MPI_Bcast(X, N * 3, MPI_FLOAT, 0, MPI_COMM_WORLD);
    }
    if (proc_id == 0) {
      calc_error();
      for (int i = 0; i < N * 3; ++i) {
        buf2[i] = X[i] < 0 ? 0 : X[i] > 255 ? 255 : X[i];
      }
      return std::make_tuple(py::array({N, 3}, buf2), py::array(3, err));
    } else {
      return std::make_tuple(py::array({1, 3}, buf2), py::array(3, err));
    }
  }
};

PYBIND11_MODULE(pie_core_mpi, m) {
  py::class_<MPIEquSolver>(m, "EquSolver")
      .def(py::init<int>())
      .def("partition", &MPIEquSolver::partition)
      .def("reset", &MPIEquSolver::reset)
      .def("sync", &MPIEquSolver::sync)
      .def("step", &MPIEquSolver::step);
}
