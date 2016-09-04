# -*- coding: utf-8 -*-
import logging
import requests
from xml.etree import ElementTree
import pprint
import json
import tempfile
import re
from jasper import plugin
#import base64
# urllib - 


INTENTS = ['switches', 'play_media', 'thermostat_set', 'scene_change']
# Required by brain.py
WORDS = INTENTS

HOST = "127.0.0.1"
STRUCTUREDEF = "/data/Loxapp3.json"
VERSION = "/dev/sps/LoxAPPversion"
SPSIO = "/dev/sps/io/"
USER = "UserName"
#HASH = "HASH"
PASSWORD = "NOPASSWORD"
TYPE_SWITCH = ["TimedSwitch","Switch"]
TYPE_LIGHTCONTROL = ["LightController"]
TYPE_JALOUSIE = ["Jalousie"]

def _handle_intent(tagged_text, mic, profile):
    """
    Parse text with attached named-entity tree
    """

    def parse_room(tree):
        room = DEFAULT_LOC
        if 'room' in tree['entities'].keys():
            room = tree['entities']['room'][0]['value']
            if room == 'bedroom':
                # speaker dependent, can't interpolate
                room = 'unknown'
        return room

    def intent_to_mqtt_switches(tree):
        ents = tree['entities']
        room = parse_room(tree)
        item = None

        if 'switch_group' in ents.keys():
            item = ents['switch_group'][0]['value']
        elif 'switch_item' in ents.keys():
            # FIXME: default to lights/*
            item = 'lights/' + ents['switch_item'][0]['value']
            if item == 'lights/amplifier':
                item = 'amp'
        else:
            item = 'lights'

        if not item:
            return (None, None)

        if 'dimmer_level' in ents.keys():
            new_state = ents['dimmer_level'][0]['value']
        else:
            new_state = ents['on_off'][0]['value']
        topic = room + '/' + item
        return (topic, new_state)

    def intent_to_mqtt_media(tree):
        ents = tree['entities']
        room = parse_room(tree)
        item = 'media' + '/' + ents['media_action'][0]['value']
        new_state = 'on'

        if ents['media_action'][0]['value'] == 'volume':
            new_state = ents['volume_percent'][0]['value']

        topic = room + '/' + item
        return (topic, new_state)

    def intent_to_mqtt_thermostat(tree):
        ents = tree['entities']
        room = parse_room(tree)
        item = 'unknown'
        if 'temperature' in ents.keys():
            item = 'setpoint'
            new_state = ents['temperature'][0]['value']

        topic = room + '/' + item
        return (topic, new_state)

    def intent_to_mqtt_scene_change(tree):
        ents = tree['entities']
        room = parse_room(tree)
        item = 'scene'
        new_state = ents['scene'][0]['value']

        topic = room + '/' + item
        return (topic, new_state)

    tree = tagged_text.tags

    logger = logging.getLogger(__name__)
    logger.debug("handle_intent: got tree=" + str(tree))

    topic = None
    new_state = None

    try:
        if tree['intent'] == 'switches':
            (topic, new_state) = intent_to_mqtt_switches(tree)
        elif tree['intent'] == 'play_media':
            (topic, new_state) = intent_to_mqtt_media(tree)
        elif tree['intent'] == 'thermostat_set':
            (topic, new_state) = intent_to_mqtt_thermostat(tree)
        elif tree['intent'] == 'scene_change':
            (topic, new_state) = intent_to_mqtt_scene_change(tree)
    except:
        mic.say(BAD_PARSE_MSG)
    else:
        if topic and new_state:
            logger.debug("ha: publishing to " + TOPIC_ROOT + topic)
            # new_state could be int, here so force conversion
            state = str(new_state)
            publish.single(TOPIC_ROOT + topic, state.upper(),
                           hostname=MQTTHOST, client_id=DEFAULT_LOC)

            mic.say(topic.replace('/', ' ') + " " + state)
        else:
            mic.say(BAD_PARSE_MSG)


class HomeLoxonePlugin(plugin.SpeechHandlerPlugin):

    def __init__(self, *args, **kwargs):
        # call super init
        super(HomeLoxonePlugin, self).__init__(*args, **kwargs)

        # read config
        try:
            self._host = self.profile['homeloxone']['host']
        except KeyError:
            self._host = HOST
        try:
            self._user = self.profile['homeloxone']['user']
        except KeyError:
            self._host = USER
        #try:
        #    self._hash = bytes(self.profile['homeloxone']['password'], "utf-8")
        #except KeyError:
        #    self._hash = HASH
        try:
            self._password = self.profile['homeloxone']['password']
        except KeyError:
            self._password = PASSWORD

        # get logger
        self._logger = logging.getLogger(__name__)

        # define request headers
        self._headers = {'accept': 'application/json'}

        # load structure definition
        r = requests.get("http://"+self._host + STRUCTUREDEF, auth=(self._user, self._password))
        try:
            r.raise_for_status()
            raw_info = r.json()['msInfo']
            raw_rooms = r.json()['rooms']
            raw_controls = r.json()['controls']
            raw_cats = r.json()['cats']
        except requests.exceptions.HTTPError:
            self._logger.critical('Request failed with response: %r',
                                  r.text,
                                  exc_info=True)
            return []
        except requests.exceptions.RequestException:
            self._logger.critical('Request failed.', exc_info=True)
            return []
        except ValueError as e:
            self._logger.critical('Cannot parse response: %s',
                                  e.args[0])
            return []
        except KeyError:
            self._logger.critical('Cannot parse response.',
                                  exc_info=True)
            return []

        # Parse structure
        try:
            # Get Info
            self._language=raw_info['languageCode']
            self._location=raw_info['location']
            self._roomtitle=raw_info['roomTitle']

            # Get rooms
            self._rooms={}            
            for room in raw_rooms:
                self._rooms[room]={"name":raw_rooms[room]['name'],"uid":raw_rooms[room]['uuid']}

            # Get categories
            self._controls={}            
            for cat in raw_cats: 
                self._controls[cat]={"name":raw_cats[cat]['name'],"uid":raw_cats[cat]['uuid'],"type":raw_cats[cat]['type'],"controls":{}}

            # fill controls
            self.extract_controls(raw_controls)    

        except KeyError:
            self._logger.critical('Cannot parse response.',
                                  exc_info=True)
        
        # Check Language
        try:
            language = self.profile['language']
        except KeyError:
            language = 'en-US'
        if language.split('-')[1]==self._language:
            raise ValueError("Home automation language is %s. But your profile language is set to %s",self._language,language)

        # debug print
        pprint.pprint(self._controls)

    def extract_controls(self, jsonconfig):
        """
        Parse the given JSON and extract the control information

        Arguments:
        jsonconfig -- controls block of the json file
        """              
        # Step though each entry
        for control in jsonconfig:
                if jsonconfig[control]['type'] in TYPE_SWITCH:
                    self._controls[jsonconfig[control]['cat']]['controls'][control]={ \
                        "name":jsonconfig[control]['name'], \
                        "uidAction":jsonconfig[control]['uuidAction'],\
                        "room":jsonconfig[control]['room'],\
                        "type":jsonconfig[control]['type']}
                elif jsonconfig[control]['type'] in TYPE_LIGHTCONTROL:
                        subcontrols=jsonconfig[control]['subControls']
                        for subcontrol in subcontrols:
                            if subcontrols[subcontrol]['type'] == "Switch":
                                self._controls[jsonconfig[control]['cat']]['controls'][subcontrol]={ \
                                "name":subcontrols[subcontrol]['name'], \
                                "uidAction":subcontrols[subcontrol]['uuidAction'],\
                                "room":jsonconfig[control]['room'],\
                                "type":subcontrols[subcontrol]['type']}
                elif jsonconfig[control]['type'] in TYPE_JALOUSIE:
                    self._controls[jsonconfig[control]['cat']]['controls'][control]={ \
                        "name":jsonconfig[control]['name'], \
                        "uidAction":jsonconfig[control]['uuidAction'],\
                        "room":jsonconfig[control]['room'],\
                        "type":jsonconfig[control]['type']}     
      
                # IRoomController
                # InfoOnlyAnalog
        return             

    def get_phrases(self):
        # extract phrases from structure definition
        phrases=[]

        # get room title
        phrases.append(self._roomtitle.encode('utf-8'))

        # get all room names
        for room in self._rooms:
            phrases.append(self._rooms[room]['name'].encode('utf-8'))

        # get control names
        for cat in self._controls:
                phrases.append(self._controls[cat]['name'].encode('utf-8'))
                controls=self._controls[cat]['controls']
                for control in controls:
                    phrases.append(controls[control]['name'].encode('utf-8'))

        # add some more phrases typically used
        # FIXME: this should be defined by intents
        phrases.append(self.gettext("switch light on off"))
        phrases.append(self.gettext("turn up down"))

        # replace none-translatable characters
        chars_to_remove = ['.', '!', '?', '(',')', '[',']', '#','{','}','_']
        rx = '[' + re.escape(''.join(chars_to_remove)) + ']'

        # for phrase in phrases:
        for index, item in enumerate(phrases):
            phrases[index]=re.sub(rx, '', item)

        # translate numbers

        print phrases
        
        return phrases


    def handle(self, text, mic):
        """
        Responds to user-input, typically speech text.

        Arguments:
        text -- user-input, typically transcribed speech
        mic -- used to interact with the user (for both input and output)
        """
        #data = fp.read()
        #r = requests.post(self._host + VERSION,
        #                  headers=self.headers)
        #try:
        #    r.raise_for_status()
        #    text = r.json()['_text']
        #except requests.exceptions.HTTPError:
        #    self._logger.critical('Request failed with response: %r',
        #                          r.text,
        #                          exc_info=True)
        #    return []
        #except requests.exceptions.RequestException:
        #    self._logger.critical('Request failed.', exc_info=True)
        #    return []
        #except ValueError as e:
        #    self._logger.critical('Cannot parse response: %s',
        #                          e.args[0])
        #    return []
        #except KeyError:
        #    self._logger.critical('Cannot parse response.',
        #                          exc_info=True)
        #    return []
        mic.say("Handle with Homeautomation") 


        

        return

    def is_valid(self, text):
        """
        Returns True if the input is related to homeautomation.

        Arguments:
        text -- user-input, typically transcribed speech
        """
    
        # FIXME: some kind of intent should be checked


        return any(p.decode('utf-8').lower() in text.lower() for p in self.get_phrases())
