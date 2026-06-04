# System Requirements

This page provides detailed hardware, software, and platform requirements to help you set up
and run the application efficiently.

## Supported Platforms

**Operating Systems**
- Ubuntu 22.04 LTS
- Ubuntu 24.04 LTS

## Minimum Requirements

| **Component**       | **Minimum Requirement** |
|---------------------|-------------------------|
| **Processor**       | 12th Generation Intel® Core™ processor and above with Intel® HD Graphics, 4th Gen Intel® Xeon® Scalable Processors   |
| **Memory**          | 8 GB                    |
| **Disk Space**      | 128 GB SSD              |
| **GPU/Accelerator** | Integrated GPU          |

### Validated Platforms

| Product / Family     | CPU |  iGPU |  NPU | dGPU |
|----------------------|-----------|------------|-----------|----------|
| Intel® Core™ Ultra Processors (Series 3, 2, 1), Intel® Core™ Processors (Series 3, 2), Intel® Core™ Processors (14th/13th/12th Gen)  | ✓         | ✓          | ✓         |  Intel(R) Arc(TM) A770, B580        |
| 4th Gen Intel® Xeon® Scalable Processors                 | ✓         |            |           | Intel(R) Arc(TM) A770, B580        |

> **Note:** Users can also create apps tailored to their use case using models supported by DL Streamer.
Check [the list of supported models](https://docs.openedgeplatform.intel.com/dev/edge-ai-libraries/dlstreamer/supported_models.html) for the latest information.

## Software Requirements

**Required Software**:
- Docker 24.0 or higher
- Python 3.10+
- Git, jq, unzip

## Compatibility notes

**Known limitations**:
- GPU optimizations require Intel® Integrated/Discrete graphics or compatible accelerators.

## Validation

- Ensure all dependencies are installed and configured before proceeding to [Get Started](../get-started.md).
