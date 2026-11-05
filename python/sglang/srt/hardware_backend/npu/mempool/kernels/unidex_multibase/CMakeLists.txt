cmake_minimum_required(VERSION 3.20)
project(unidex_multibase LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
add_compile_options(-O3 -fvisibility-inlines-hidden -fstack-protector-strong -ftrapv)
include(${SGL_SOURCE}/cmake/config_envs.cmake)
include(${SGL_SOURCE}/cmake/config_ascend.cmake)
ascendc_library(unidex_multibase_device STATIC kernel.cpp)
ascendc_include_directories(unidex_multibase_device PRIVATE ${SGL_SOURCE}/csrc/utils/kernel)
add_library(unidex_multibase SHARED host.cpp)
target_link_libraries(unidex_multibase PRIVATE unidex_multibase_device torch_npu ascendcl)
target_link_directories(unidex_multibase PRIVATE ${TORCH_DIR}/lib ${TORCH_NPU_DIR}/lib)
target_link_options(unidex_multibase PRIVATE -Wl,--exclude-libs,ALL -Wl,-z,noexecstack -Wl,-z,relro -Wl,-z,now)
target_include_directories(unidex_multibase PRIVATE
    ${SGL_SOURCE}/csrc/utils ${TORCH_DIR}/include ${TORCH_DIR}/include/torch/csrc/api/include
    ${TORCH_NPU_DIR}/include ${ASCEND_INCLUDE_DIR} ${ASCEND_INCLUDE_DIR}/external
    ${ASCEND_INCLUDE_DIR}/experiment/platform ${ASCEND_INCLUDE_DIR}/experiment/runtime
    ${ASCEND_DRIVER_PATH}/include ${ASCEND_DRIVER_PATH}/kernel/inc)
