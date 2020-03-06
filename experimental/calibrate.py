import visa
import serial
import serial.tools.list_ports
import time
import math
import statistics
import win32api
import os.path
import shutil
import io
import subprocess


METER_RESOURCE_NAME = "USB0::0x05E6::0x6500::04450405::INSTR"
SOL_USB_DEVICE_ID = "239A:8062"
METER_AVERAGE_COUNT = 1  # 50 for production
MILIVOLTS_PER_CODE = 13 / (2**16) * 1000

class Meter:
    TIMEOUT = 10 * 1000

    def __init__(self, resource_manager):
        self._connect(resource_manager)

    def _connect(self, resource_manager):
        resource = resource_manager.open_resource(METER_RESOURCE_NAME)
        resource.timeout = self.TIMEOUT
        self.port = resource

    def close(self):
        self.port.close()

    def read_voltage(self):
        self.port.write("*RST")
        self.port.write(':SENS:FUNC "VOLT:DC"')
        self.port.write(":SENS:VOLT:RANG 10")
        self.port.write(":SENS:VOLT:INP AUTO")
        self.port.write(":SENS:VOLT:NPLC 1")
        self.port.write(":SENS:VOLT:AZER ON")
        self.port.write(":SENS:VOLT:AVER:TCON REP")
        self.port.write(f":SENS:VOLT:AVER:COUN {METER_AVERAGE_COUNT}")
        self.port.write(":SENS:VOLT:AVER ON")
        return self.port.query_ascii_values(":READ?")[0]



class Sol:
    VERBOSE = False
    DAC_SETTLING_TIME = 0.1

    def __init__(self):
        self._connect()

    def _connect(self):
        port_info = list(serial.tools.list_ports.grep(SOL_USB_DEVICE_ID))[0]
        self.port = serial.Serial(
            port_info.device,
            baudrate=115200,
            timeout=1)
    
    def reset(self):
        time.sleep(2)
        self.port.write(b'\x03')
        time.sleep(2)
        self.port.write(b'\x04')
        while True:
            line = self.port.readline().decode("utf-8").strip()
            if self.VERBOSE:
                print("Sol: ", line)
            if line == "ready":
                break
            if line.startswith("Press any key to enter the REPL. Use CTRL-D to reload."):
                time.sleep(1)
                self.port.write(b'\x04')
            pass

    def call(self, expr):
        self.port.write(f"{expr}\r\n".encode("utf-8"))
        self.port.flush()
        output = ''
        while True:
            line = self.port.readline().decode("utf-8").strip()
            if self.VERBOSE:
                print("Sol: ", line)
            if line == "done":
                break
            if line.startswith("Traceback"):
                error = self.port.read(size=500).decode("utf-8")
                raise RuntimeError(f"Error while interacting with Sol: {error}")
            
            if not line.startswith(expr):
                output += line
        
        return output

    def set_dac(self, channel, dac_code):
        self.call(f"set_dac('{channel}', {dac_code})")
        time.sleep(self.DAC_SETTLING_TIME)

    def set_voltage(self, channel, voltage):
        self.call(f"set_voltage('{channel}', {voltage})")
        time.sleep(self.DAC_SETTLING_TIME)

    def set_calibration(self, channel, calibration_values):
        self.call(f"set_calibration('{channel}', {calibration_values})")

    def get_cpu_id(self):
        return self.call("get_cpu_id()")

    def write_calibration_to_nvm(self, calibration_data):
        self.call(f"write_calibration_to_nvm(\"\"\"{calibration_data}\"\"\")")


def find_circuitpython_drive():
    drives = win32api.GetLogicalDriveStrings()
    drives = drives.split('\000')[:-1]
    for drive in drives:
        info = win32api.GetVolumeInformation(drive)
        if info[0] == "CIRCUITPY":
            return drive
    raise RuntimeError("No circuitpython drive found.")


def copyfile(src, dst):
    # shutil can be a little wonky, so do this manually.
    with open(src, "r") as fh:
        contents = fh.read()

    with open(dst, "w") as fh:
        fh.write(contents)
        fh.flush()

    drive, _ = os.path.splitdrive(dst)
    subprocess.run(["sync", drive], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def copy_calibration_script(circuitpython_drive):
    print(f"Copying calibration script to {circuitpython_drive}")
    copyfile(
        os.path.join(os.path.dirname(__file__), "calibration_cpy_code.py"),
        os.path.join(circuitpython_drive, "code.py"))


def generate_calibration_file(channel_calibrations):
    buf = io.StringIO()
    buf.write("calibration = {")
    for channel, calibration in channel_calibrations.items():
        buf.write(f"\"{channel}\": {calibration},\n")
    buf.write("}\n")
    return buf.getvalue()


circuitpython_drive = find_circuitpython_drive()
resource_manager = visa.ResourceManager("@ni")
meter = Meter(resource_manager)
sol = Sol()
copy_calibration_script(circuitpython_drive)
time.sleep(5)  # Wait a few second for circuitpython to maybe reload.
sol.reset()
cpu_id = sol.get_cpu_id()

print(f"Sol CPU ID: {cpu_id}")

channel_calibrations = {}
channel_voltages = {}

try:
    for channel in ("a", "b", "c", "d"):
        print(f"========= Channel {channel} =========")
        input(f"Connect to channel {channel}, press enter when ready.")
        calibration_values = {}
        channel_voltages[channel] = {}

        for step in range(16):
            dac_code = int((2**16 - 1) * step / 15)
            sol.set_dac(channel, dac_code)
            voltage = meter.read_voltage()
            calibration_values[voltage] = dac_code
            print(f"DAC code: {dac_code}, Voltage: {voltage}")

        sol.set_calibration(channel, calibration_values)
        channel_calibrations[channel] = calibration_values

        for desired_voltage in range(-5, 9):
            sol.set_voltage(channel, desired_voltage)
            measured_voltage = meter.read_voltage()
            channel_voltages[channel][desired_voltage] = measured_voltage
            print(f"Desired voltage: {desired_voltage}, Measured voltage: {measured_voltage}")

    
    calibration_file_contents = generate_calibration_file(channel_calibrations)
    sol.write_calibration_to_nvm(calibration_file_contents)

finally:
    meter.close()

print("========= Stats =========")
for channel, voltages in channel_voltages.items():
    if not voltages:
        continue

    print(f"Channel {channel}:")
    differences = [abs(desired - measured) for desired, measured in voltages.items()]
    avg = statistics.mean(differences) * 1000
    dev = statistics.stdev(differences) * 1000
    worst = max(differences) * 1000
    best = min(differences) * 1000
    print(f"Average: {avg:.3f} mV ({avg / MILIVOLTS_PER_CODE:.0f} lsb)")
    print(f"Std. dev: {dev:.3f} mV ({dev / MILIVOLTS_PER_CODE:.0f} lsb)")
    print(f"Worst: {worst:.3f} mV ({worst / MILIVOLTS_PER_CODE:.0f} lsb)")
    print(f"Best: {best:.3f} mV ({best / MILIVOLTS_PER_CODE:.0f} lsb)")


print(f"Saving calibration to calibrations/{cpu_id} and {circuitpython_drive}")

calibration_file_path = os.path.join(os.path.dirname(__file__), 'calibrations', f'{cpu_id}.py')

with open(calibration_file_path, "w") as fh:
    fh.write("# This is generated by the factory when assembling your\n")
    fh.write("# device. Do not remove or change this.\n\n")
    fh.write(calibration_file_contents)
    fh.flush()

copyfile(calibration_file_path, os.path.join(circuitpython_drive, "calibration.py"))

print("Done.")