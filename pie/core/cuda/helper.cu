#include "helper.h"

CudaEquSolver::CudaEquSolver(int block_size)
    : buf(NULL),
      buf2(NULL),
      block_size(block_size),
      cA(NULL),
      cB(NULL),
      cX(NULL),
      tmp(NULL),
      EquSolver() {
  print_cuda_info();
  cudaMalloc(&cerr, 3 * sizeof(float));
}

CudaEquSolver::~CudaEquSolver() {
  if (buf != NULL) {
    delete[] buf, buf2;
  }
  if (tmp != NULL) {
    cudaFree(cA);
    cudaFree(cB);
    cudaFree(cX);
    cudaFree(tmp);
  }
  cudaFree(cerr);
}

py::array_t<int> CudaEquSolver::partition(py::array_t<int> mask) {
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

void CudaEquSolver::post_reset() {
  if (cA != NULL) {
    delete[] buf2;
    cudaFree(cA);
    cudaFree(cB);
    cudaFree(cX);
    cudaFree(cbuf);
    cudaFree(tmp);
  }
  buf2 = new unsigned char[N * 3];
  cudaMalloc(&cA, N * 4 * sizeof(int));
  cudaMalloc(&cB, N * 3 * sizeof(float));
  cudaMalloc(&cX, N * 3 * sizeof(float));
  cudaMalloc(&cbuf, N * 3 * sizeof(unsigned char));
  cudaMalloc(&tmp, N * 3 * sizeof(float));
  cudaMemcpy(cA, A, N * 4 * sizeof(int), cudaMemcpyHostToDevice);
  cudaMemcpy(cB, B, N * 3 * sizeof(float), cudaMemcpyHostToDevice);
  cudaMemcpy(cX, X, N * 3 * sizeof(float), cudaMemcpyHostToDevice);
}

__global__ void iter_kernel(int N0, int N1, int* A, float* B, float* X) {
  int i = blockIdx.x * blockDim.x + threadIdx.x + N0;
  if (i < N1) {
    int off3 = i * 3;
    int4 id = (*((int4*)(A + i * 4))) * 3;
    float3 x = *((float3*)(B + off3));
    if (id.x) {
      x += *((float3*)(X + id.x));
    }
    if (id.y) {
      x += *((float3*)(X + id.y));
    }
    if (id.z) {
      x += *((float3*)(X + id.z));
    }
    if (id.w) {
      x += *((float3*)(X + id.w));
    }
    *((float3*)(X + off3)) = x / 4.0;
  }
}

__global__ void iter_shared_kernel(int N0, int N1, int* A, float* B, float* X) {
  __shared__ float sX[4096 * 3];  // max shared size
  int i = blockIdx.x * blockDim.x + threadIdx.x + N0;
  if (i < N1) {
    int i0 = blockIdx.x * blockDim.x + N0;
    int i1 = (1 + blockIdx.x) * blockDim.x + N0;
    if (i1 > N1) i1 = N1;
    int off0 = i0 * 3;
    int off1 = i1 * 3;
    int off3 = i * 3;

    // load X to shared mem
    // sX[0..(i1 - i0), :] = X[i0..i1, :]
    *((float3*)(sX + off3 - off0)) = *((float3*)(X + off3));
    __syncthreads();

    int4 id = (*((int4*)(A + i * 4))) * 3;
    float3 x = *((float3*)(B + off3));
    if (id.x) {
      if (off0 <= id.x && id.x < off1) {
        x += *((float3*)(sX + id.x - off0));
      } else {
        x += *((float3*)(X + id.x));
      }
    }
    if (id.y) {
      if (off0 <= id.y && id.y < off1) {
        x += *((float3*)(sX + id.y - off0));
      } else {
        x += *((float3*)(X + id.y));
      }
    }
    if (id.z) {
      if (off0 <= id.z && id.z < off1) {
        x += *((float3*)(sX + id.z - off0));
      } else {
        x += *((float3*)(X + id.z));
      }
    }
    if (id.w) {
      if (off0 <= id.w && id.w < off1) {
        x += *((float3*)(sX + id.w - off0));
      } else {
        x += *((float3*)(X + id.w));
      }
    }
    *((float3*)(X + off3)) = x / 4.0;
  }
}

__global__ void error_kernel(int N0, int N1, int* A, float* B, float* X,
                             float* tmp) {
  int i = blockIdx.x * blockDim.x + threadIdx.x + N0;
  if (i < N1) {
    int off3 = i * 3;
    int4 id = (*((int4*)(A + i * 4))) * 3;
    float3 t = (*((float3*)(X + off3))) * 4.0 - (*((float3*)(B + off3))) -
               (*((float3*)(X + id.x))) - (*((float3*)(X + id.y))) -
               (*((float3*)(X + id.z))) - (*((float3*)(X + id.w)));
    *((float3*)(tmp + off3)) = fabs(t);
  }
}

__global__ void error_sum_kernel(int N, int block_size, float* tmp,
                                 float* err) {
  __shared__ float sum_err[4096 * 3];  // max shared size
  int id = blockIdx.x * blockDim.x + threadIdx.x;
  float3 err3 = make_float3(0.0, 0.0, 0.0);
  for (int i = id; i < N; i += block_size) {
    err3 += *((float3*)(tmp + i * 3));
  }
  *((float3*)(sum_err + id * 3)) = err3;
  __syncthreads();
  if (id == 0) {
    err3 = make_float3(0.0, 0.0, 0.0);
    for (int i = 0; i < block_size; ++i) {
      err3 += *((float3*)(sum_err + i * 3));
    }
    *(float3*)(err) = err3;
  }
}

__global__ void copy_X_kernel(int N0, int N1, float* X, unsigned char* buf) {
  int i = blockIdx.x * blockDim.x + threadIdx.x + N0;
  if (i < N1) {
    buf[i] = X[i] < 0 ? 0 : X[i] > 255 ? 255 : X[i];
  }
}

std::tuple<py::array_t<unsigned char>, py::array_t<float>> CudaEquSolver::step(
    int iteration) {
  cudaMemset(cerr, 0, 3 * sizeof(float));
  int grid_size = (N - 1 + block_size - 1) / block_size;
  for (int i = 0; i < iteration; ++i) {
    iter_kernel<<<grid_size, block_size>>>(1, N, cA, cB, cX);
    // doesn't occur any numeric issue ...
    // cudaDeviceSynchronize();
  }
  cudaDeviceSynchronize();
  grid_size = (N * 3 - 3 + block_size - 1) / block_size;
  copy_X_kernel<<<grid_size, block_size>>>(3, 3 * N, cX, cbuf);
  grid_size = (N - 1 + block_size - 1) / block_size;
  error_kernel<<<grid_size, block_size>>>(1, N, cA, cB, cX, tmp);
  cudaDeviceSynchronize();
  error_sum_kernel<<<1, block_size>>>(N, block_size, tmp, cerr);
  cudaDeviceSynchronize();

  cudaMemcpy(err, cerr, 3 * sizeof(float), cudaMemcpyDeviceToHost);
  cudaMemcpy(buf2, cbuf, 3 * N * sizeof(unsigned char), cudaMemcpyDeviceToHost);
  return std::make_tuple(py::array({N, 3}, buf2), py::array(3, err));
}
