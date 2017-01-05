from queue import Queue
from collections import deque
from logging import getLogger
from threading import Thread
from sqlalchemy.exc import DBAPIError
from time import time
from random import shuffle

import asyncio

from utils import dump_pickle, load_pickle, get_current_hour, time_until_time, round_coords, get_altitude, get_point_altitudes

import db

class Spawns:
    """Manage spawn points and times"""
    session = db.Session()
    spawns = {}
    despawn_times = {}
    mysteries = set()
    altitudes = {}

    def __len__(self):
        return len(self.despawn_times)

    def update(self):
        self.spawns, self.despawn_times, m, a = db.get_spawns(self.session)
        self.mysteries.update(m)
        self.altitudes.update(a)
        if not self.altitudes:
            self.altitudes = get_point_altitudes()

    def have_id(self, spawn_id):
        return spawn_id in self.despawn_times

    def get_altitude(self, point):
        point = round_coords(point)
        alt = self.altitudes.get(point)
        if not alt:
            alt = get_altitude(point)
            self.altitudes[point] = alt
        return alt

    def items(self):
        return self.spawns.items()

    def get_mysteries(self):
        mysteries = deque(self.mysteries)
        shuffle(mysteries)
        return mysteries

    def add_mystery(self, point):
        rounded = round_coords(point, precision=4)
        self.mysteries.add(rounded)

    def have_mystery(self, point):
        rounded = round_coords(point, precision=4)
        return rounded in self.mysteries

    def add_despawn(self, spawn_id, despawn_time):
        self.despawn_times[spawn_id] = despawn_time

    def get_despawn_seconds(self, spawn_id):
        return self.despawn_times.get(spawn_id)

    def get_despawn_time(self, spawn_id, seen=None):
        if self.have_id(spawn_id):
            now = seen or time()
            hour = get_current_hour(now=now)
            despawn_time = self.get_despawn_seconds(spawn_id) + hour
            if now > despawn_time - 88:
                despawn_time += 3600
            return despawn_time
        else:
            return None

    def get_time_till_hidden(self, spawn_id):
        if not self.have_id(spawn_id):
            return None
        despawn_seconds = self.get_despawn_seconds(spawn_id)
        return time_until_time(despawn_seconds)

    @property
    def total_length(self):
        return len(self.despawn_times) + len(self.mysteries)


class DatabaseProcessor(Thread):
    spawns = Spawns()

    def __init__(self):
        super().__init__()
        self.queue = Queue()
        self.logger = getLogger('dbprocessor')
        self.running = True
        self._clean_cache = False
        self.count = 0
        self._commit = False

    def stop(self):
        self.running = False

    def add(self, obj):
        self.queue.put(obj)

    def run(self):
        session = db.Session()

        while self.running or not self.queue.empty():
            if self._clean_cache:
                try:
                    db.SIGHTING_CACHE.clean_expired()
                    db.MYSTERY_CACHE.clean_expired(session)
                except Exception as e:
                    self.logger.error('Failed to clean cache. {}'.format(e))
                finally:
                    self._clean_cache = False
            try:
                item = self.queue.get()

                if item['type'] == 'pokemon':
                    if item['valid']:
                        db.add_sighting(session, item)
                        if item['valid'] == True:
                            db.add_spawnpoint(session, item, self.spawns)
                    else:
                        db.add_mystery(session, item, self.spawns)
                    self.count += 1
                elif item['type'] == 'fort':
                    db.add_fort_sighting(session, item)
                elif item['type'] == 'pokestop':
                    db.add_pokestop(session, item)
                elif item['type'] == 'kill':
                    break
                self.logger.debug('Item saved to db')
                if self._commit:
                    session.commit()
                    self._commit = False
            except DBAPIError as e:
                session.rollback()
                self.logger.exception('A wild DB exception appeared! {}'.format(e))
            except Exception as e:
                self.logger.exception('A wild exception appeared! {}'.format(e))

        session.commit()
        session.close()

    def clean_cache(self):
        self._clean_cache = True

    def commit(self):
        self._commit = True