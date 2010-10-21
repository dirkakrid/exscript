# Copyright (C) 2007-2010 Samuel Abels.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
import threading, time, gc
from itertools                     import chain
from collections                   import defaultdict
from Exscript.external.SpiffSignal import Trackable
from Job                           import Job

class MainLoop(Trackable, threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        Trackable.__init__(self)
        self.queue            = []
        self.force_start      = []
        self.running_jobs     = []
        self.sleeping_actions = []
        self.paused           = True
        self.shutdown_now     = False
        self.max_threads      = 1
        self.condition        = threading.Condition()
        self.debug            = 0
        self.setDaemon(1)

    def _dbg(self, level, msg):
        if self.debug >= level:
            print msg

    def _action_sleep_notify(self, action):
        assert self.in_progress(action)
        self.condition.acquire()
        self.sleeping_actions.append(action)
        self.condition.notifyAll()
        self.condition.release()

    def _action_wake_notify(self, action):
        assert self.in_progress(action)
        assert action in self.sleeping_actions
        self.condition.acquire()
        self.sleeping_actions.remove(action)
        self.condition.notifyAll()
        self.condition.release()

    def get_max_threads(self):
        return self.max_threads

    def set_max_threads(self, max_threads):
        assert max_threads is not None
        self.condition.acquire()
        self.max_threads = int(max_threads)
        self.condition.notifyAll()
        self.condition.release()

    def enqueue(self, action):
        action._mainloop_added_notify(self)
        self.condition.acquire()
        self.queue.append(action)
        self.condition.notifyAll()
        self.condition.release()

    def enqueue_or_ignore(self, action):
        action._mainloop_added_notify(self)
        self.condition.acquire()
        if not self.get_first_action_from_name(action.name):
            self.queue.append(action)
            enqueued = True
        else:
            enqueued = False
        self.condition.notifyAll()
        self.condition.release()
        return enqueued

    def priority_enqueue(self, action, force_start = False):
        action._mainloop_added_notify(self)
        self.condition.acquire()
        if force_start:
            self.force_start.append(action)
        else:
            self.queue.insert(0, action)
        self.condition.notifyAll()
        self.condition.release()

    def priority_enqueue_or_raise(self, action, force_start = False):
        self.condition.acquire()

        # If the action is already running (or about to be forced),
        # there is nothing to be done.
        running_actions = self.get_running_actions()
        for queue_action in chain(self.force_start, running_actions):
            if queue_action.name == action.name:
                self.condition.notifyAll()
                self.condition.release()
                return False

        # If the action is already in the queue, remove it so it can be
        # re-added later.
        existing = None
        for queue_action in self.queue:
            if queue_action.name == action.name:
                existing = queue_action
                break
        if existing:
            self.queue.remove(existing)
            action = existing
        else:
            action._mainloop_added_notify(self)

        # Now add the action to the queue.
        if force_start:
            self.force_start.append(action)
        else:
            self.queue.insert(0, action)

        self.condition.notifyAll()
        self.condition.release()
        return existing is None

    def pause(self):
        self.condition.acquire()
        self.paused = True
        self.condition.notifyAll()
        self.condition.release()

    def resume(self):
        self.condition.acquire()
        self.paused = False
        self.condition.notifyAll()
        self.condition.release()

    def is_paused(self):
        return self.paused

    def wait_for(self, action):
        self.condition.acquire()
        while self.in_queue(action):
            self.condition.wait()
        self.condition.release()

    def wait_for_activity(self):
        self.condition.acquire()
        self.condition.wait(.2)
        self.condition.release()

    def wait_until_done(self):
        self.condition.acquire()
        while self.get_queue_length() > 0:
            self.condition.wait()
        self.condition.release()

    def shutdown(self):
        self.condition.acquire()
        self.shutdown_now = True
        self.condition.notifyAll()
        self.condition.release()
        for job in self.running_jobs:
            job.join()
            self._dbg(1, 'Job "%s" finished' % job.getName())

    def in_queue(self, action):
        return action in self.queue \
            or action in self.force_start \
            or self.in_progress(action)

    def in_progress(self, action):
        return action in self.get_running_actions()

    def get_running_actions(self):
        return [job.action for job in self.running_jobs]

    def get_queue_length(self):
        #print "Queue length:", len(self.queue)
        return len(self.queue) \
             + len(self.force_start) \
             + len(self.running_jobs)

    def get_actions_from_name(self, name):
        actions = self.queue + self.force_start + self.running_jobs
        map     = defaultdict(list)
        for action in self.workqueue.get_running_actions():
            map[action.get_name()].append(action)
        return map[name]

    def get_first_action_from_name(self, action_name):
        for action in chain(self.queue, self.force_start, self.running_jobs):
            if action.name == action_name:
                return action
        return None

    def _start_action(self, action):
        job = Job(self.condition, action, debug = self.debug)
        self.running_jobs.append(job)
        job.start()
        self._dbg(1, 'Job "%s" started.' % job.getName())
        try:
            self.signal_emit('job-started', job)
        except:
            pass

    def _on_job_completed(self, job):
        try:
            if job.exception:
                self._dbg(1, 'Job "%s" aborted.' % job.getName())
                self.signal_emit('job-aborted', job, job.exception)
            else:
                self._dbg(1, 'Job "%s" succeeded.' % job.getName())
                self.signal_emit('job-succeeded', job)
        except:
            pass
        try:
            self.signal_emit('job-completed', job)
        except:
            pass

    def _update_running_jobs(self):
        # Update the list of running jobs.
        running   = []
        completed = []
        for job in self.running_jobs:
            if job.is_alive():
                running.append(job)
                continue
            completed.append(job)
        self.running_jobs = running[:]

        # Notify any clients *after* removing the job from the list.
        for job in completed:
            self._on_job_completed(job)
            job.join()
            del job
        gc.collect()

    def run(self):
        self.condition.acquire()
        while not self.shutdown_now:
            self._update_running_jobs()
            if self.get_queue_length() == 0:
                self.signal_emit('queue-empty')

            # If there are any actions to be force_started, run them now.
            for action in self.force_start:
                self._start_action(action)
            self.force_start = []
            self.condition.notifyAll()

            # Don't bother looking if the queue is empty.
            if len(self.queue) <= 0 or self.paused:
                self.condition.wait()
                continue

            # Wait until we have less than the maximum number of threads.
            active = len(self.running_jobs) - len(self.sleeping_actions)
            if active >= self.max_threads:
                self.condition.wait()
                continue

            # Take the next action and start it in a new thread.
            action = self.queue.pop(0)
            self._start_action(action)
            self.condition.release()

            if len(self.queue) <= 0:
                self._dbg(2, 'No more pending actions in the queue.')
            self.condition.acquire()
        self.condition.release()
        self._dbg(2, 'Main loop terminated.')
