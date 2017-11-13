#! /usr/bin/env python

# btrfs-clone: clones a btrfs file system to another one
# Copyright (C) 2017 Martin Wilck
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import sys
import re
import os
import subprocess
import atexit
import tempfile
import gzip
from uuid import uuid4
from argparse import ArgumentParser
from stat import ST_DEV
from time import sleep
import traceback

opts = None
VERBOSE = []

def randstr():
    return str(uuid4())[-12:]

def check_call(*args, **kwargs):
    if opts.verbose:
        print (" ".join(args[0]))
    if not opts.dry_run:
        subprocess.check_call(*args, **kwargs)

def prop_get_ro(path, yesno):
    info = subprocess.check_output([opts.btrfs, "property", "get", "-ts",
                                    path, "ro"])
    info = info.rstrip()
    return info == "ro=true"

def prop_set_ro(path, yesno):
    check_call([opts.btrfs, "property", "set", "-ts",
                path, "ro", "true" if yesno else "false"])

class Subvol:

    # Max diff between "generation" and "generation of origin" which
    # is considered "static" (aka read-only snapshot)
    MAX_STATIC = 1

    class NoSubvol(ValueError):
        pass
    class BadId(RuntimeError):
        pass
    class MissingAttr(RuntimeError):
        pass

    def __init__(self, mnt, path):
        self.mnt = mnt
        self.path = path
        self._init_from_show()

    def _init_from_show(self):
        info = subprocess.check_output([opts.btrfs, "subvolume", "show",
                                        "%s/%s" % (self.mnt, self.path)])
        for line in info.split("\n"):
            try:
                k, v = line.split(":", 1)
            except ValueError:
                continue
            k = k.strip()
            v = v.strip()
            if k == "UUID":
                self.uuid = v
            elif k == "Parent UUID":
                self.parent_uuid = v
                if self.parent_uuid == "-":
                    self.parent_uuid = None
            elif k == "Subvolume ID":
                self.id = int(v)
            elif k == "Parent ID":
                self.parent_id = int(v)
            elif k == "Generation":
                self.gen = int(v)
            elif k == "Gen at creation":
                self.ogen = int(v)
            elif k == "Flags":
                self.ro = (v.find("readonly") != -1)

        for attr in ("parent_id", "parent_uuid", "ro", "gen", "ogen", "uuid"):
            if not hasattr(self, attr):
                raise self.MissingAttr("%s: no %s" % (self, attr))

    def __str__(self):
        return ("%s(%d)" % (self.path, self.id))

    def is_static(self):
        return (self.gen - self.ogen <= self.MAX_STATIC)

    def longstr(self):
        return (("subvol %d gen %d->%d %s UUID=%s ro:%s" +
                 "\n\tParent: %d %s") %
                (self.id, self.ogen, self.gen, self.path, self.uuid, self.ro,
                 self.parent_id, self.parent_uuid))

    def get_mnt(self, mnt = None):
        if mnt is None:
            return self.mnt
        return mnt

    def get_path(self, mnt = None):
        return "%s/%s" % (self.get_mnt(mnt), self.path)

    def get_ro(self, mnt = None):
        return prop_get_ro(self.get_path(mnt))

    def ro_str(self, mnt = None, prefix=""):
        return ("%s%s (%s): %s" % (prefix, self.path, self.ro,
                                   self.get_ro(mnt)))

    def set_ro(self, yesno, mnt = None):
        # Never change a subvol that was already ro
        if self.ro:
            return
        return prop_set_ro(self.get_path(mnt), yesno)

def get_subvols(mnt):
    vols = subprocess.check_output([opts.btrfs, "subvolume", "list",
                                    "-t", "--sort=ogen",
                                    mnt])
    svs = []
    for line in vols.split("\n"):
        # Skip header lines
        if line is "" or not line[0].isdigit():
            continue
        try:
            sv = Subvol(mnt, line.split()[3])
        except (Subvol.NoSubvol, IndexError):
            pass
        except:
            raise
        else:
            svs.append(sv)
    return svs

def umount_root_subvol(dir):
    try:
        subprocess.check_call(["umount", "-l", dir])
        os.rmdir(dir)
    except:
        pass

def mount_root_subvol(mnt):
    td = tempfile.mkdtemp()
    info = subprocess.check_output([opts.btrfs, "filesystem", "show", mnt])
    line = info.split("\n")[0]
    uuid = re.search(r"uuid: (?P<uuid>[-a-f0-9]*)", line).group("uuid")
    subprocess.check_call(["mount", "-o", "subvolid=5", "UUID=%s" % uuid, td])
    atexit.register(umount_root_subvol, td)
    return (uuid, td)

def set_all_ro(yesno, subvols, mnt = None):
    if yesno:
        l = subvols
    else:
        l = reversed(subvols)

    for sv in l:
        try:
            sv.set_ro(yesno, mnt = mnt)
        except subprocess.CalledProcessError:
            if not yesno:
                print ("Error setting ro=%s for %s: %s") % (
                    yesno, sv.path, sys.exc_info()[1])
                continue
            else:
                raise

def do_send_recv(old, new, send_flags=[]):
    send_cmd = ([opts.btrfs, "send"] + VERBOSE + send_flags + [old])
    recv_cmd = ([opts.btrfs, "receive"] + VERBOSE + [new])

    if opts.verbose > 1:
        name = new.replace("/", "-")
        recv_name = "btrfs-recv-%s.log.gz" % name
        send_name = "btrfs-send-%s.log.gz" % name
        recv_log = gzip.open(recv_name, "wb")
        send_log = gzip.open(send_name, "wb")
    else:
        recv_log = subprocess.PIPE
        send_log = subprocess.PIPE

    if opts.verbose:
        print ("%s |\n\t %s" % (" ".join(send_cmd), " ".join(recv_cmd)))
    if opts.dry_run:
        return

    try:
        send = subprocess.Popen(send_cmd, stdout=subprocess.PIPE,
                                stderr=send_log)
        recv = subprocess.Popen(recv_cmd, stdin=send.stdout,
                                stderr=recv_log)
        send.stdout.close()
        recv.communicate()
        send.wait()
    finally:
        if opts.verbose > 1:
            recv_log.close()
            send_log.close()

    if recv.returncode != 0 or send.returncode != 0:
        if opts.verbose > 1:
            print ("please check %s and %s" % (send_name, recv_name))
        else:
            if send.returncode != 0:
                print ("Error in send:\n%s" % send.stderr.read())
            if recv.returncode != 0:
                print ("Error in recv:\n%s" % recv.stderr.read())
        raise RuntimeError("Error in send/recv for %s -> %s" % (old, new))

def send_root(old, new):
    name = randstr()
    old_snap = "%s/%s" % (old, name)
    new_snap = "%s/%s" % (new, name)
    subprocess.check_call([opts.btrfs, "subvolume", "snapshot", "-r", old, old_snap])
    atexit.register(subprocess.check_call,
                    [opts.btrfs, "subvolume", "delete", old_snap])
    do_send_recv(old_snap, new)
    check_call([opts.btrfs, "property", "set", new_snap, "ro", "false"])

    dir = old_snap if opts.dry_run else new_snap
    dev = os.lstat(dir)[ST_DEV]
    if opts.toplevel:
        for el in os.listdir(dir):
            path = "%s/%s" %(dir, el)
            dev1 = os.lstat(path)[ST_DEV]
            if dev != dev1:
                continue
            # Can' use os.rename here (cross device link)
            check_call(["mv", "-f", "-t", new] +
                       (["-v"] if opts.verbose else []) + [path])
        check_call([opts.btrfs, "subvolume", "delete", new_snap])
        ret = new
    else:
        ret = new_snap
        print ("top level subvol in clone is: %s" % name)
    return ret

def send_subvol_parent(subvol, get_parents, old, new):
    ancestors = [[ "-c", x.get_path(old) ] for x in get_parents(subvol)]
    c_flags = [x for anc in ancestors for x in anc]
    if ancestors:
        p_flags = [ "-p", ancestors[0][1] ]
    else:
        p_flags = []
    do_send_recv(subvol.get_path(old), os.path.dirname(subvol.get_path(new)),
                 send_flags = p_flags + c_flags)


def parents_getter(subvols):
    lookup = { x.uuid: x for x in subvols }
    def _getter(x):
        while x.parent_uuid is not None:
            try:
                x = lookup[x.parent_uuid]
            except KeyError:
                return
            else:
                yield x
    return _getter

def send_subvols_parent(old_mnt, new_mnt, subvols):
    # A snapshot always has higher ogen than its source
    subvols.sort(key = lambda x: (x.ogen, x.id))

    get_parents = parents_getter(subvols)
    new_subvols = []

    for sv in subvols:
        send_subvol_parent(sv, get_parents, old_mnt, new_mnt)
        sv.set_ro(False, new_mnt)
        #if not opts.dry_run:
        #    print (sv.ro_str(new_mnt))
        new_subvols.append(sv)

def send_subvol_chrono(sv, subvols, old, sv_base, parent=None):

    snaps = [x for x in subvols if x.parent_uuid == sv.uuid]
    snaps.sort(key = lambda x: (x.ogen, x.id))

    prev = None
    for snap in snaps:
        send_subvol_chrono(snap, subvols, old, sv_base, parent=prev)
        prev = snap

    if parent is None:
        parent = prev
        prev = None
    if parent is not None:
        flags = [ "-p", parent.get_path(old), "-c", parent.get_path(old)]
    else:
        flags = []
    if prev is not None:
        flags += [ "-c", prev.get_path(old) ]

    sv_base.send(sv, old, flags)

def send_subvol_snap(sv, subvols, old, sv_base, parent=None):

    if parent is not None:
        flags = [ "-p", parent.get_path(old), "-c", parent.get_path(old)]
    else:
        flags = []

    sv_base.send(sv, old, flags)

    snaps = [x for x in subvols if x.parent_uuid == sv.uuid]
    snaps.sort(reverse = True, key = lambda x: (x.ogen, x.id))

    prev = sv
    for snap in snaps:
        send_subvol_snap(snap, subvols, old, sv_base, parent=prev)
        prev = snap

def move_to_tree_pos(sv, new, sv_base, done):
    goal = sv.get_path(new)
    last = os.path.basename(goal)
    dir = sv_base.sv_dir(sv)
    cur = "%s/%s" % (dir, last)

    if opts.dry_run:
        check_call(["mv", "-f", cur, os.path.dirname(goal)])
        return

    if not os.path.isdir(cur):
        if os.path.isdir(goal):
            print ("ah, %s already moved" % goal)
            return True
        else:
            print ("ERROR: %s was not created" % cur)
            return False
    elif sv.parent_id == 5 or sv.parent_id in done:
        if sv.ro:
            prop_set_ro(cur, False)
        try:
            check_call(["mv", "-f", cur, os.path.dirname(goal)])
        finally:
            if sv.ro:
                try:
                    if os.path.isdir(goal):
                        prop_set_ro(goal, True)
                except:
                    pass
                try:
                    if os.path.isdir(cur):
                        prop_set_ro(cur, True)
                except:
                    pass
        try:
            os.rmdir(dir)
        except OSError:
            print ("Failed to remove %s (this is non-fatal)" % dir)
        done.add(sv.id)
        return True
    else:
        print ("Hmm, parent %d of %d not found" % (sv.parent_id, sv.id))
        return False

class SvBaseDir:
    def __init__ (self, new, subvols):
        self.base = "%s/%s" % (
            new, opts.snap_base if opts.snap_base else randstr())
        self.new = new
        self.subvols = subvols

    def __enter__(self):
        if not opts.dry_run and not os.path.isdir(self.base):
            os.mkdir(self.base)
        return self

    def __exit__(self, *args):
        self.subvols.sort(key = lambda x: (x.parent_id, x.id))
        done = set()
        for sv in self.subvols:
            move_to_tree_pos(sv, self.new, self, done)
        if not opts.dry_run:
            try:
                os.rmdir(self.base)
            except OSError:
                print ("Failed to remove %s (this is non-fatal)" % self.base)

    def sv_dir(self, sv):
        return "%s/%s" % (self.base, sv.id)

    def send(self, sv, old, flags):
        dir = self.sv_dir(sv)
        path = sv.get_path(old)
        newpath = "%s/%s" % (dir, os.path.basename(path))
        if not opts.dry_run and not os.path.isdir(dir):
            os.mkdir(dir)
        if os.path.isdir(newpath):
            print ("%s exists, not sending" % newpath)
        else:
            do_send_recv(path, dir, send_flags = flags)
            if not sv.ro and not opts.dry_run:
                prop_set_ro(newpath, False)

def send_subvols_snap(old, new, subvols):

    lookup = parents_getter(subvols)
    with SvBaseDir(new, subvols) as sv_base:
        for sv in (x for x in subvols if (x.parent_uuid is None or
                                          lookup(x.parent_uuid) is None)):
            if opts.strategy  == "snapshot":
                send_subvol_snap(sv, subvols, old, sv_base)
            elif opts.strategy  == "chronological":
                send_subvol_chrono(sv, subvols, old, sv_base)

def get_parent(sv, subvols):
    for s in subvols:
        if s.uuid == sv.parent_uuid:
            return s
    return None

def get_first(lst, fn):
    for x in (y for y in lst if fn(y)):
        return x
    return None

def get_max(lst, sel, key):
    l = [y for y in lst if sel(y)]
    if not l:
        return None
    return max(l, key = key)

def get_min(lst, sel, key):
    l = [y for y in lst if sel(y)]
    if not l:
        return None
    return min(l, key = key)

def pr_list(msg,lst):
    if opts.verbose > 1:
        print ("%s: %s" % (msg, ", ".join(str(x) for x in lst)))

def select_best_ancestor(sv, get_ancestors, done):

    # Consider the following history tree.
    #
    # Lines denote evolvement of a subvolume in time.
    # Crosses are "forks" (creation of r/w subvolumes).
    # "*" denotes "static" (ro) snapshots, "o" non-static (rw).
    # Generation increases vertically top-down.
    #
    #                                   |
    #                    /--------------+ (5)
    #                    |              |
    #         /----------+              G
    #         |          |
    #         |          * a
    #         |          |
    #         |        3 +--------\
    #       e o          |        |
    #             /------+ 1      |
    #             |      |        |
    #             |    4 +---\    o b
    #             |      |   |
    #             |      |   o c
    #        /--- + 2    |
    #        |    |      * d
    #        |    |      |
    #      C o    |      |
    #             |      o M
    #             |
    #             o S
    #
    # We are looking at S. C is a snapshot of S, M is a snaphot of G.
    # All other subvolumes (including S itself) are snapshots of (some
    # former state of) M.
    #
    # IOW: M is "mom" of S, C a child of S, G "grandma" of S,
    # all others are siblings of S (being snapshots of M).
    #
    # Because "generation" strategy cloes subvols ordered by generation
    # all nodes except S have already been cloned.
    #
    # Which subvols should be used as clone sources, and which one
    # of them should be the best "parent" for btrfs-send? btrfs-receive
    # will create a snapshot of the "parent" on the target side, and
    # modify this snapshot, using data from all clone sources, until
    # it matches S. If we included all nodes except S in the set of
    # clone sources and didn't set "-p" explicitly, btrfs-send would
    # choose M as parent.

    def selection(best, reason):
        clone_sources.add(best)
        if None in clone_sources:
            clone_sources.remove(None)
        if opts.verbose > 0:
            print("%s <= %s (reason: %s); %s" %
                  (sv, best, reason,
                   ", ".join(str(s) for s in clone_sources)))
        return (best, clone_sources)

    # "done" should be sorted by gen already because of the way it's build
    # up in send_subvols_gen(), but let's be paranoid
    done.sort(key = lambda x: (x.gen, x.id), reverse=True)

    clone_sources = set()
    best_static_child = None
    mom = ancestor = None

    children = [s for s in done if s.parent_uuid == sv.uuid]
    pr_list("children of %s" % sv, children)
    if children:
        best_static_child = get_first(children, lambda x: x.is_static())
        if best_static_child is not None:
            clone_sources.union([x for x in children
                                 if x.ogen > best_static_child.ogen])
            return selection(best_static_child, "static child")
        else:
            # non-static children can be VERY different, don't use as "best"
            clone_sources.update(children)

    # a parent's gen is not necessarily lower than the child's gen
    # but there may be older ancestors (grandparents etc.) with lower gen
    # Get the one that's closed to us in terms of ogen
    ancestors = [ x for x in get_ancestors(sv) ]
    pr_list("ancestors of %s" % sv, ancestors)
    if ancestors:
        # node M in tree above
        mom = ancestors[0]
        # node G in tree above
        ancestor = get_max(ancestors, lambda x: x in done,
                           lambda x: x.ogen)
        if ancestor is not None:
            clone_sources.add(ancestor)
            if ancestor is mom:
                return selection(mom, "mom")
        siblings = [x for x in done if x.parent_uuid == mom.uuid]
        pr_list("siblings of %s" % sv, siblings)
    else:
        siblings = []

    # There may be more siblings, but we look only at those that
    # are cloned already (are members of done)
    if not siblings:
        if ancestor is not None:
            return selection(ancestor, "ancestor")
        else:
            return selection(None, "orphan")

    # Don't call me a sexist please... This is easier to remember
    # and less confusing than "older_siblings" etc.
    brothers = [x for x in siblings if x.ogen < sv.ogen]
    pr_list("brothers of %s" % sv, brothers)
    sisters = [x for x in siblings if x.ogen >= sv.ogen]
    pr_list("sisters of %s" % sv, sisters)

    # node a in tree above
    youngest_static_brother = get_max(brothers, lambda x: x.is_static(),
                                      lambda x: x.ogen)
    # also node a
    youngest_brother = get_max(brothers, lambda x: x.gen < sv.ogen,
                               lambda x: x.ogen)
    # node b
    youngest_brother_ogen = get_max(brothers, lambda x: True,
                                    lambda x: x.ogen)

    # node d
    oldest_static_sister = get_min(sisters, lambda x: x.is_static(),
                                   lambda x: x.ogen)
    # node c
    oldest_sister = get_min(sisters, lambda x: True,
                            lambda x: x.ogen)

    # also node c
    oldest_sister_gen = get_min(sisters, lambda x: True,
                                lambda x: x.gen)

    # By using a set here, we automatically avoid duplicates.
    # "None" is removed in selection()
    clone_sources.add(youngest_static_brother)
    clone_sources.add(youngest_brother)
    clone_sources.add(youngest_brother_ogen)
    clone_sources.add(oldest_static_sister)
    clone_sources.add(oldest_sister)
    clone_sources.add(oldest_sister_gen)

    if youngest_static_brother is not None:
        return selection(youngest_static_brother, "static brother")

    if oldest_static_sister is not None:
        return selection(oldest_static_sister, "static sister")

    if youngest_brother is not None:
        return selection(youngest_brother, "youngest brother")

    if ancestor is not None and ancestor.is_static():
        return selection(ancestor, "static ancestor")

    candidates = set([ancestor, youngest_brother_ogen,
                      oldest_sister, oldest_sister_gen])
    if None in candidates:
        candidates.remove(None)

    if candidates:
        return selection(min(candidates, key = lambda x: abs(x.ogen - sv.ogen)),
                         "nicest relative")

    return selection(None, "no nice relatives")

def send_subvol_gen(sv, get_ancestors, old, sv_base, done):
    (best, clone_sources) = select_best_ancestor(sv, get_ancestors, done)

    flags = [f for c in clone_sources for f in ("-c", c.get_path(old))]
    if best is not None:
        flags += ["-p", best.get_path(old)]

    sv_base.send(sv, old, flags)

def send_subvols_gen(old, new, subvols):
    subvols.sort(key = lambda x: (x.gen, x.id))
    get_ancestors = parents_getter(subvols)

    done = []
    with SvBaseDir(new, subvols) as sv_base:
        for sv in subvols:
            send_subvol_gen(sv, get_ancestors, old, sv_base, done)
            done = [sv] + done

def send_subvols(old_mnt, new_mnt):
    subvols = get_subvols(old_mnt)
    atexit.register(set_all_ro, False, subvols, old_mnt)
    set_all_ro(True, subvols, old_mnt)

    if opts.strategy == "parent":
        send_subvols_parent(old_mnt, new_mnt, subvols)
    elif opts.strategy == "snapshot" or opts.strategy == "chronological":
        send_subvols_snap(old_mnt, new_mnt, subvols)
    elif opts.strategy == "generation":
        send_subvols_gen(old_mnt, new_mnt, subvols)

def make_args():
    ps = ArgumentParser()
    ps.add_argument("-v", "--verbose", action='count', default=0)
    ps.add_argument("-B", "--btrfs", default="btrfs")
    ps.add_argument("-f", "--force", action='store_true')
    ps.add_argument("-n", "--dry-run", action='store_true')
    ps.add_argument("-s", "--strategy", default="snapshot",
                    choices=["parent", "snapshot", "chronological",
                             "generation"])
    ps.add_argument("--snap-base")
    ps.add_argument("--no-unshare", action='store_true')
    ps.add_argument("-t", "--toplevel", action='store_false',
                    help="clone toplevel into a subvolume")
    ps.add_argument("old")
    ps.add_argument("new")
    return ps

def parse_args():
    global opts
    global VERBOSE

    ps = make_args()
    opts = ps.parse_args()
    if opts.verbose is not None:
        VERBOSE = ["-v"] * opts.verbose

def main():
    parse_args()

    if not opts.no_unshare:
        print ("unsharing mount namespace")
        os.execvp("unshare", ["unshare", "-m"] + sys.argv + ["--no-unshare"])

    (old_uuid, old_mnt) = mount_root_subvol(opts.old)
    (new_uuid, new_mnt) = mount_root_subvol(opts.new)

    msg = None
    if (old_uuid == new_uuid):
        msg = ("%s and %s are the same file system" %
               (opts.old, opts.new))
    if len(os.listdir(new_mnt)) > 0:
        msg = "fileystem %s is not empty" % opts.new

    if msg is not None and not opts.dry_run:
        if not opts.force:
            raise RuntimeError(msg)
        else:
            print ("*** WARNING ***: %s" % msg)
            print ("Hit ctrl-c within 10 seconds ...")
            sleep(10)

    if (opts.verbose > 0):
        print ("OLD btrfs %s mounted on %s" % (old_uuid, old_mnt))
        print ("NEW btrfs %s mounted on %s" % (new_uuid, new_mnt))

    new_mnt = send_root(old_mnt, new_mnt)
    send_subvols(old_mnt, new_mnt)

if __name__ == "__main__":
    try:
        main()
    except:
        if opts.verbose > 1:
            traceback.print_exc()
        else:
            print ("%s" % sys.exc_info()[1])
