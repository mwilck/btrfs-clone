# btrfs-clone

This program clones an existing BTRFS file system to a new one,
cloning each subvolume in order.
Thanks to Thomas Luzat for the [original idea][1] 

**This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.**

## Usage:

    btrfs-clone [options] <mount-point-of-existing-FS> <mount-point-of-new-FS>

## Options:

 * `--verbose`: can be repeated. For high verbose levels, btrfs send/receive
   output is saved in the working directory
 * `--force`: proceed in possibly dangerous conditions
 * `--dry-run`: do no actual transfers
 * `--strategy`: either "parent" or "snapshot" (default), see below
 * `--toplevel`: don't try to write the target toplevel subvolume, see below

## Example for real-world use:

    mkfs.btrfs /dev/sdb1
    mkdir /mnt/new
    mount /dev/sdb1 /mnt/new
    btrfs-clone.py / /mnt/new

## General remarks:

The new file system should be large enough to receive all data
from the old one. The tool does not check this.

The new filesystem should ideally be newly created, and have a distinct
UUID from the one to be cloned. The `--force` option allows to attempt
cloning even if this is not the case.

The two file systems don't need to be mounted by the toplevel subvolume, the
program will remount the top subvolumes on temporary mount points.

Error handling is pretty basic. This program relies on the btrfs tools
to fail, and will abort if that happens (for example, btrfs-receive
refuses to overwrite existing subvolumes, which is a good thing). The tool
doesn't attempt to continue after cloning a certain subvolume failed.
As long as you clone to a fresh file system, this tool can't do much
harm to your system. The FS to be cloned is only touched for creating a
snapshot of the toplevel volume (see "toplevel" below).

During the cloning, all subvolumes of the origin FS are set to read-only.
Thus if cloning your root fs, make sure there isn't much other stuff going
on in the system.

### Why not just use rsync?

rsync lacks knowledge of the btrfs file system internals (shared extents)
and will thus waste a lot of disk space in the presence of snapshots.

## Cloning by strategies: "parent" vs. "snapshot"

Consider the following typical topology, decreasing chronological order,
where the current fs tree has been snapshotted several times in the past:

    current ------------------------------------
	             |       |        |          |
	           snap4   snap3    snap2      snap1

With "parent" strategy (which was Thomas' original proposal), we'd clone
"current" first, and after that the snapshots one by one, using "current"
as "parent" (["-p" option to btrfs-send][2]) for every snapshot.

But obviously the similarity between snap1 and snap2 will be much higher then
between snap1 and current. This will cause a waste of disk space, as shared
extents can't be used efficiently.

"snapshot" strategy uses the "neighbour snapshot" as parent.
We clone "current" first, then snap4 with `-p current`, snap3 with `-p snap4`,
etc. This ensures that differences are as small as possible. We could have
done it in reverse order as well (starting with "snap1"), but that would
cause the clone of "current" to appear as a snapshot, which sounds weird.

Snapshot strategy has the side effect that the parent-child relationships (expressed by
`parent_uuid`) are different in the cloned file system compared to the original
(snap3 will appear to be a snapshot of snap4, whereas it was a snapshot of
current in the original FS). Also, file systems will not be cloned in the
order of their creation, thus when we clone a subvolume, we can't be sure that
its parent in the filesystem tree (btrfs `parent_id`, don't confuse with
`parent_uuid`) has already been transferred. Therefore we clone all subvolumes
in flat topology into a temporary directory first. When all subvolumes are
cloned, they are moved into their desired fs tree position.

I implemented "snapshot" strategy because I once tried to clone a BTRFS file 
system with ~20GB of 40GB used with the "parent" strategy, and failed because 
the receiving file system (40GB raw space) had filled up. With "snapshot" 
strategy, I ended up with ~23GB used. That indicates that there's obviously still
room for improvement.

## The --toplevel option

The toplevel "subvolume" of a BTRFS file system can't be cloned with
send/receive. It's only possible to create a snapshot of the toplevel
FS and clone that. Obviously, the cloned snapshot in the new FS will be
distinct from the toplevel of the new FS. By default, this tool moves the
content of the cloned snapshot to the toplevel of the new FS and deletes
the snapshot. If this is not desired, the `--toplevel` option can be used.
It causes the tool to keep the cloned snapshot volume and create all
subvolumes relative to it.

[1]: https://superuser.com/questions/607363/how-to-copy-a-btrfs-filesystem
[2]: https://btrfs.wiki.kernel.org/index.php/FAQ#What_is_the_difference_between_-c_and_-p_in_send.3F
