🛠️ Configuration Guide

1. Create the Configuration File
Create a file named configuration.txt in the same directory as the Python script.

Example format:
ini
Copy
Edit
[DEFAULT]
RootDirectory = C:/Your/Desired/Output/Folder

[Setup0]
CameraID = 0
COMPort = COM3

[Setup1]
CameraID = 1
COMPort = COM4
CameraID: Index of the USB camera (0, 1, etc. — verify with a test script).

COMPort: The COM port of the Arduino associated with that camera (e.g., COM3).

Add as many setups as needed using [Setup2], [Setup3], etc.

🧪 How the Program Works

2. Startup Behavior
Verifies the root directory exists. If not, prompts you to create it.

Checks each camera can be opened. If any cannot, it exits with an error.

Warns once if any specified COM ports (Arduinos) are not available, and uses simulated input for those.

🖥️ Using the GUI

Components:
Mouse ID: Text box to enter a unique ID for each mouse.

Session Duration (min): Set how long (in minutes) you plan to record. Recording does not auto-stop.

Elapsed / Remaining Time:

Updates live.

Turns red if the elapsed time exceeds the set duration.

Start / Stop: Begin or end recording for the current setup.

< / > Buttons: Navigate between different setups.

Auto Cycle: When enabled, automatically rotates through setups.

Dwell (s): How many seconds to stay on each setup when auto cycling.

📁 Output Structure

When recording:

Creates a directory:
RootDirectory/MouseID/sleep_cam/YYYYMMDD_HHMMSS/

Inside this folder:

setupX.mp4: The recorded video.

setupX.csv: A log of timestamps and Arduino data.

⚠️ Notes

You must manually stop each recording.

Auto cycle is just for viewing — it does not affect recording logic.

Each setup retains its own Mouse ID and session duration across switching.
