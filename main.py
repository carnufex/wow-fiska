import pyaudio
import os
from collections import deque
import audioop
import math
import time
from pyclick import HumanClicker, HumanCurve
import cv2
import random
import pyautogui

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

class HumanClickerParent(HumanClicker):
    '''overriding class'''
    def move_and_search(self, toPoint, duration=2, humanCurve=None):
        from_point = pyautogui.position()
        if not humanCurve:
            humanCurve = HumanCurve(from_point, toPoint)

        pyautogui.PAUSE = duration / len(humanCurve.points)
        x, y = pyautogui.size()
        start_x = x/3
        start_y = y/2
        i=0
        for point in humanCurve.points:
            pyautogui.moveTo(point)
            i += 1
            if i % 2 == 0:
                flote = imageSearch.imagesearcharea('assets/flote.png', (x/6)*5, (y/5)*4, x, y, 0.8)
                if flote[0] is not -1:
                    print("found blobb")
                    return point
        return (-1, -1)

        

class Fiskare:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.device_info = {}
        self.useloopback = False
        self.defaultframes = 512
        self.fish_sound = False
        self.channelcount = 0
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

        self.channelcount = self.device_info["maxInputChannels"] if (self.device_info["maxOutputChannels"] < self.device_info["maxInputChannels"]) else self.device_info["maxOutputChannels"]

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
        self.fish_sound = False
        #Open stream
        stream = self.p.open(format = pyaudio.paInt16,
                        channels = self.channelcount,
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
                    # print("sound")
                    self.fish_sound = True
                    time.sleep(rand(1, 0.5))
                    break
                elif time.time() - start_time > 30:
                    # print("listened for 30 seconds, aborting")
                    break
            except:
                print("exception")
                break

        stream.stop_stream()
        stream.close()

    def stop(self):
        #Close module
        self.p.terminate()
        
def sweep_flote(hc):
    x, y = pyautogui.size()
    start_x = x/3
    start_y = y/2
    end_x = (x/3)*2
    end_y = (y/3)*2
    offset_x = 1
    current_x = start_x - rand(60, 30)
    current_y = start_y - rand(60, 30)
    while current_y < end_y:
        flote = hc.move_and_search((int(current_x), int(current_y)), 0)
        if flote[0] is not -1:
            return flote
        if current_x > end_x:
            offset_x = -1
        elif current_x < start_x:
            offset_x = 1
        current_x = current_x + (end_x-start_x + rand(121, 30)) * offset_x
        current_y = current_y + rand(40, 10)
    return (-1, -1)



def rand(num, rnd):
    return num + rnd*random.random()

def start_meta():
    pyautogui.press('0')

def satt_pa_mask():
    pyautogui.press('9')
    time.sleep(rand(2, 1))
    pyautogui.press('9')
    time.sleep(rand(6, 1))

def main():
    fails = 0
    fish = 0
    gubbe = Fiskare()
    gubbe.init()
    hc = HumanClickerParent()
    try:
        pos = rand(700, 500)
        pyautogui.moveTo(pos, pos)
        pyautogui.click()
        satt_pa_mask()
        start_meta()
        start_time = time.time()
        while True:
            if time.time() - start_time > rand(600, 30):
                satt_pa_mask()
                start_time = time.time()
                start_meta()
            found_flote = sweep_flote(hc)
            if found_flote[0] is not -1:
                gubbe.listen()
                if gubbe.fish_sound:
                    pyautogui.click(button='right')
                    fish += 1
                    time.sleep(rand(4, 1))
                    start_meta()
                else:
                    fails += 1
                    start_meta()
            else:
                fails += 1
                start_meta()
            print("Fishes found: {} \n Fails: {}".format(fish, fails))
            
    except KeyboardInterrupt:
        print("Stopped.")
        gubbe.stop()
    gubbe.stop()

main()