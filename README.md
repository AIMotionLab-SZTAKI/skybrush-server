libmotioncapture extension and SZTAKI-aimotionlab extension for Skybrush Server
==============================================================================

This repository contains an experimental version of the Skybrush Server
that adds support for multiple mocap systems via an abstraction layer offered
by `libmotioncapture` for indoor drone tracking, as well as SZTAKI's Aimotionlab extension.
This readme contains info about installation and setup. For information about
how the extension works, visit the Wiki.

Before we begin
------------

1. You will need to install the driver for the Crazyradio Dongle. Instructions 
   on it can be found here: https://www.bitcraze.io/products/crazyradio-pa/

2. You will need Git and Poetry. You probably already have Git downloaded, but you
   may need to install Poetry. If you are using Windows, you're going to have to add the path where
   poetry was installed to your Path environmental variable, so pay attention to where 
   it was installed. Before continuing on, in order to make poetry install the virtual 
   environment in your project folder (instead of deep in AppData), run this command: 
   `poetry config virtualenvs.in-project true`.
   
3. You will need a drone with Skybrush compatible firmware. Instructions on achieving
   this can be found here: https://github.com/AIMotionLab-SZTAKI/crazyflie-firmware.
   Do not forget to designate a marker set for the drone in Motive!
   
4. Download skybrush live (AppImage for Linux, executable for Windows):
   https://skybrush.io/modules/live/


Installation
------------

1. Check out this repository using git.
2. For the Skybrush server to work with the optitrack system, you need the libmotioncapture package. For this, we need 
   to tell poetry where to look for the files of the package.
   1. On python 3.8 or 3.9: open pyproject.toml, and under `[tool.poetry.dependencies]` , look
      for the line `motioncapture = ...`. Change it to `motioncapture = "^1.0a1"`. This will
      install the libmotioncapture library from pypi. 
   2. If you're using python 3.10 or newer, this won't work, because (as of the writing of this readme) the version of 
      libmotioncapture on pypi is not compatible with the newer python versions. Instead, we need to compile our own 
      libmotioncapture package for our version of python. Note that this is only easily achieved on Linux. 
      Check out the libmotioncapture repository: https://github.com/IMRCLab/libmotioncapture. In the libmotioncapture 
      directory, do `git submodule init` & `git submodule update`. You may also need to install these packages (and their dependencies):
      `sudo apt install libboost-system-dev libboost-thread-dev libeigen3-dev ninja-build`
      In the same directory, `python3 -m build`. This creates the necessary
      wheel file in the libmotioncapture/dist directory. Open pyproject.toml, and under `[tool.poetry.dependencies]` 
      look for the line `motioncapture = { file = "..."}`. Change the path here to wherever your wheel
      for libmotioncapture can be found (for example, libmotioncapture/dist/WHEELNAME.whl).

3. Run `poetry install`; this will create a virtual environment and install
   Skybrush Server with all required dependencies in it, as well as the code
   of the extensions.

4. If any dependencies fail to install at first, you may check their status
   with `poetry show`. It is possible that some *optional* dependencies may fail to install.
   If this is the case, you can try deleting these dependencies from pyproject.toml (they are
   marked with optional=true). 
   
5. Make sure you are connected to the optitrack server **via ethernet**. Wireless connection
   will result in choppy data transfer.

6. Run the server with `poetry run skybrushd -c skybrushd.jsonc`.

7. Start Skybrush Live. When you start Live, the server terminal should tell you that a
   Client is connected. You should be able to see any turned on Drones under UAVs. Before
   doing a takeoff, make sure that the position of the drone is stable. If there is an
   issue with the motion capture system, the drone's position will diverge. If you turned
   on the drone's tracking in motive *after* the server was launched, you need to restart
   the server.