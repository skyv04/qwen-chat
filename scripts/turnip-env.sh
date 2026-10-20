# Source this for a Mesa build with freedreno's KGSL backend.
export MESA_ADRENO_PREFIX="${MESA_ADRENO_PREFIX:-/opt/mesa-adreno}"
export VK_DRIVER_FILES="$MESA_ADRENO_PREFIX/share/vulkan/icd.d/freedreno_icd.aarch64.json"
if [ ! -r "$VK_DRIVER_FILES" ]; then
    echo "qwen: Turnip ICD not found: $VK_DRIVER_FILES. Build Mesa with KGSL support first." >&2
    return 1
fi
export VK_ICD_FILENAMES="$VK_DRIVER_FILES"
export LD_LIBRARY_PATH="$MESA_ADRENO_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LIBGL_DRIVERS_PATH="$MESA_ADRENO_PREFIX/lib/dri"
export MESA_LOADER_DRIVER_OVERRIDE=zink
export GALLIUM_DRIVER=zink
export __GLX_VENDOR_LIBRARY_NAME=mesa
export MESA_VK_WSI_DEBUG=sw
unset LIBGL_ALWAYS_SOFTWARE
