#include "solver.h"

#include <tuple>

#include "helper.h"

PYBIND11_MODULE(pie_core_cuda, m) {
  py::class_<CudaEquSolver>(m, "EquSolver")
      .def(py::init<int>())
      .def("partition", &CudaEquSolver::partition)
      .def("reset", &CudaEquSolver::reset)
      .def("sync", &CudaEquSolver::sync)
      .def("step", &CudaEquSolver::step);
  py::class_<CudaGridSolver>(m, "GridSolver")
      .def(py::init<int, int>())
      .def("reset", &CudaGridSolver::reset)
      .def("sync", &CudaGridSolver::sync)
      .def("step", &CudaGridSolver::step);
}
