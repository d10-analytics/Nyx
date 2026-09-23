[app]

# Standalone GUI deployment inputs for the private desktop application.  The
# build driver resolves the placeholder paths for the current checkout,
# interpreter, and platform before invoking the deployment tool.  The committed
# file keeps the structural deployment choices under review: standalone mode,
# QtWebEngine resources, and application-named output.

# Title of the application.
title = Nyx

# Project root scanned for imported Qt modules.
project_dir = @PROJECT_DIR@

# Python entry point for the standalone GUI.
input_file = @INPUT_FILE@

# Directory that receives the finalized executable or app bundle.
exec_directory = @EXEC_DIRECTORY@

project_file =
icon =

[python]

# Interpreter that runs the deployment tool and its pinned packages.
python_path = @PYTHON_PATH@

# Pinned deployment toolchain.
packages = Nuitka==4.1.1

android_packages =

[qt]

qml_files =

# Left empty so QtWebEngine is not excluded from a non-QML application.
excluded_qml_plugins =

# Left empty so the deployment tool discovers the imported Qt modules.
modules =

plugins =

[nuitka]

macos.permissions =

# Standalone keeps the engine process and its resources in one directory.
mode = standalone

# Reproducible build arguments.  The platform placeholder carries the
# console-window choice, and the static placeholder carries the absolute
# location of the served board assets.
extra_args = --quiet --noinclude-qt-translations --include-data-dir=@STATIC_SOURCE@=nyx/static @PLATFORM_ARGS@

[buildozer]

mode = debug

recipe_dir =
jars_dir =
ndk_path =
sdk_path =
local_libs =
arch =
