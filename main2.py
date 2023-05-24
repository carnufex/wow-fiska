import pyaudio
import os
from collections import deque
import audioop
import math
import time

from lib import utilities, windowTitles, sendInput, imageSearch


class textcolors:
    if not os.name == 'nt':
        blue = '\033[94m'
        green = '\033[92m'
        warning = '\033[93m'
        fail = '\033[91m'
        end = '\033[0m'
    else:
        blue = ''
        green = ''
        warning = ''
        fail = ''
        end = ''

class Fiskare:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.device_info = {}
        self.useloopback = False
        self.defaultframes = 512
        self.fishSound = False
        #Use module
        self.p = pyaudio.PyAudio()
        
    def init(self):
        #Set default to first in list or ask Windows
        try:
            default_device_index = self.p.get_default_input_device_info()
        except IOError:
            default_device_index = -1

        #Select Device
        print (textcolors.blue + "Available devices:\n" + textcolors.end)
        for i in range(0, self.p.get_device_count()):
            info = self.p.get_device_info_by_index(i)
            print (textcolors.green + str(info["index"]) + textcolors.end + ": \t %s \n \t %s \n" % (info["name"], self.p.get_host_api_info_by_index(info["hostApi"])["name"]))

            if default_device_index == -1:
                default_device_index = info["index"]

        #Handle no devices available
        if default_device_index == -1:
            print (textcolors.fail + "No device available. Quitting." + textcolors.end)
            exit()


        #Get input or default
        device_id = int(input("Choose device [" + textcolors.blue + str(default_device_index) + textcolors.end + "]: ") or default_device_index)
        print ("")

        #Get device info
        try:
            self.device_info = self.p.get_device_info_by_index(device_id)
        except IOError:
            self.device_info = self.p.get_device_info_by_index(default_device_index)
            print (textcolors.warning + "Selection not available, using default." + textcolors.end)

        #Choose between loopback or standard mode
        is_input = self.device_info["maxInputChannels"] > 0
        is_wasapi = (self.p.get_host_api_info_by_index(self.device_info["hostApi"])["name"]).find("WASAPI") != -1
        if is_input:
            print (textcolors.blue + "Selection is input using standard mode.\n" + textcolors.end)
        else:
            if is_wasapi:
                self.useloopback = True;
                print (textcolors.green + "Selection is output. Using loopback mode.\n" + textcolors.end)
            else:
                print (textcolors.fail + "Selection is input and does not support loopback mode. Quitting.\n" + textcolors.end)
                exit()

    def listen(self):
        #Open stream
        channelcount = self.device_info["maxInputChannels"] if (self.device_info["maxOutputChannels"] < self.device_info["maxInputChannels"]) else self.device_info["maxOutputChannels"]
        stream = self.p.open(format = pyaudio.paInt16,
                        channels = channelcount,
                        rate = int(self.device_info["defaultSampleRate"]),
                        input = True,
                        frames_per_buffer = self.defaultframes,
                        input_device_index = self.device_info["index"],
                        as_loopback = self.useloopback)

        slid_win = deque(maxlen=1 * int(self.device_info["defaultSampleRate"]/self.defaultframes))
        start_time = time.time()
        while True:
            try:
                slid_win.append(math.sqrt(abs(audioop.avg(stream.read(self.defaultframes), 4))))
                if(sum([x > 1000 for x in slid_win]) > 0): #1000 for fishing max volume?
                    print("sound")
                    return True
                elif time.time() - start_time > 30:
                    print("listened for 30 seconds, aborting")
                    return False
            except:
                print("exception")
                print(slid_win)
                break

        stream.stop_stream()
        stream.close()
        #Close module
        self.p.terminate()

def find_flote():
    flote = imageSearch.imagesearch('/assets/flote.png')
    if flote[0] is not -1:
        print("found the flote")
    else:
        print("nothing found")

def main():
    gubbe = Fiskare()
    gubbe.init()
    found_fish = gubbe.listen()
    if found_fish:
        find_flote()
    else:
        print("didnt trigger from sound")
