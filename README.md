# btrfs-clone

This program clones an existing BTRFS file system to a new one,
cloning each subvolume in order. Thanks to Thomas Luzat for the [original idea][1].

**This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.**

## Usage:

    btrfs-clone [options] <mount-point-of-existing-FS> <mount-point-of-new-FS>

## Options:

 * `--verbose`: increase verbosity level. This option can be repeated. For
   verbose levels >=2, btrfs send/receive output is saved in the working
   directory, and python tracebacks are printed upon exceptions.
 * `--force`: proceed in possibly dangerous conditions.
 * `--dry-run`: do no actual transfer data. It's recommended to run this first
   together with `-v` and examine the output to see what would be done.
 * `--ignore-errors`: continue after errors in send/receive. May be useful for
   backing up corrupted file systems. Make sure to check results.
 * `--strategy`: either "parent", "snapshot", "chronological",
   "generation" (default), or "bruteforce"; see below.
 * `--toplevel`: don't try to write the target toplevel subvolume, see below.
 * `--btrfs`: set full path to "btrfs" executable.

## Example for real-world use:

    mkfs.btrfs /dev/sdb1
    mkdir /mnt/new
    mount /dev/sdb1 /mnt/new
    btrfs-clone.py / /mnt/new

## Alternatives

If the source and target file system size match, and both are single-device
file systems, good old **dd** may be reasonable alternative method to transfer
a btrfs file system from one drive to another. Another approach, based on
**btrfs device** operations, is outlined in [Moving my butter][4]. Yet a
different approach would be to use **btrfs replace**. The two latter variants
destroy the source file system though, they're only really use full for file
system *migration*.

One problem with all these approaches is that they also
clone the file system UUID, therefore the devices with original and cloned file system
can't be present in the same system at the same time without confusing the
kernel. This problem can be overcome by running **btrfstune -u** after the cloning operation.

Tools like **rsync**, which are not aware of btrfs file system internals, will
waste disk space in the presence of snapshots, because they can't take
advantage of shared extents.

## General remarks

 * The new filesystem should ideally be newly created, and have a distinct
 UUID from the one to be cloned. The `--force` option allows to attempt
 cloning even if this is not the case.
 * Both source and target file systems must be mounted, but they don't need to
 be mounted by the toplevel subvolume. The program will remount the top
 subvolumes on temporary mount points.
 * The tool does not check beforehand if the new file system is large enough
 to hold all data. Overflow of the target file system causes the cloning
 procedure to fail, but should not do any harm to the system.
 * This tool should be pretty safe to use. The source file system is only
 touched for creating a snapshot of the toplevel volume (see "toplevel"
 below).  During cloning, all subvolumes of the origin FS are set to read-only
 mode. Thus if cloning your root fs, make sure there isn't much other stuff
 going on in the system.
 * The tool cleans up after exit, e.g. read-only flags for subvolumes in the
 source file system are restored to their original state on exit.

### Checking data integrity

It's recommended to run something like

    rsync -n -avxAHXS <src-subvol> <dst-subvol>

for every subvolume cloned to make sure that the cloning actually produced
a 1:1 copy of the original data.

## Cloning strategies

Btrfs send/receive has been designed for incremental backup scenarios where
the user exactly knows the previous snapshot to compare against. In the
scenario that this tool is trying to solve, it's not always obvious against
which existing subvolume to use to record incremental changes. This isn't
dangerous for data integrity; it may just lead to suboptimal usage of
space in the cloned file system. This tool implements different strategies to
determine reference subvolumes for each subvolume cloned.

### Child-parent relationship

Except for the "parent" and "bruteforce" strategy, the child-parent
relationships ("C is a snapshot of P") in the target file system will
be different from those in the source file system. If 3rd party tools
rely on a certain parent-child relationship, only the "parent"
strategy can be used.  I tried this with **snapper**, and it seemed to
work fine with a clone generated with the "generation" strategy;
apparently it only relies on its own meta data, which is preserved in
the cloning procedure.

Moreover, file systems will not be cloned in the order of their creation, thus
when a subvolume is cloned, we can't be sure that its parent in the filesystem
tree (btrfs `parent_id`, don't confuse with `parent_uuid`) has already been
transferred. Therefore subvolumes are first cloned flatly into a temporary
directory. After all subvolumes have been transferred, they are moved into
their file position in the target filesystem tree.

### Space efficiency

I started implemententing the different strategies after realizing that the
obvious "parent" strategy could yield suboptimal results. The following table
summarizes results I got for an aged filesystem hosting a Linux root FS using
**snapper**, on a 40GB device with 21.00Gib used:

| strategy      | size after cloning      |
|---------------|------------------------:|
| parent        | >40GB (target overflow) |
| snapshot      | 23GB                    |
| chronological | 23GB                    |
| generation    | 20.5GB                  |

In general, the canonical "parent" strategy is recommended when parent-child
relationship needs to be preserved, but it's least space efficient. "snapshot"
and "chronological" and "generation" work well for linear history (a file
system with some read-only snapshots representing former states, with no
branches or rollbacks). Complex history is handled best by "generation", but
of course there's no guarantee that results will always be optimal.

### "parent" strategy

"parent" strategy uses the subvolume's `parent_uuid` to determine the
subvolume used as parent for **btrfs-send**.

Consider the following typical topology, decreasing chronological order,
where the current fs tree (the default subvolume) has been snapshotted
several times in the past:

    current ---------------------------------\
                 |       |        |          |
               snap4   snap3    snap2      snap1

With "parent" strategy (which was Thomas' original proposal), we'd clone
"current" first, and after that the snapshots one by one, using "current"
both as "parent" and "clone source" (["-p" option to btrfs-send][2]) for every
snapshot.

### "bruteforce" strategy

This strategy is similar to "parent". But it uses every "relative" of
the subvolume to be cloned as clone source, rather than just the
direct parent. The set of "relatives" contains all ancestors and all
descendants of all ancestors. This may lead to a rather large set of
clone sources, slowing down **btrfs send** operation.

Like "parent", this strategy preserves the child-parent
relationships. As outlined above, that may be suboptimal for meta data
cloning. But data cloning should be pretty good with this method, as
every possible clone source is taken into account.

### "snapshot" strategy

Obviously, in the picture above, the similarity between snap1 and snap2 will
be much higher then between snap1 and current. This will cause a waste of disk
space, as shared extents can't be used efficiently.

The "snapshot" strategy uses the "oldest sibling snapshot" as reference device
rather than the "parent". Thus in the example above, we clone "current" first,
then snap4 with `-p current`, snap3 with `-p snap4`, etc. This ensures that
differences are smaller than for "parent" strategy, and yields an overall
better efficiency for linear history.

### "chronological" strategy

This is essentially the same as "snapshot" strategy, but parent relationships
on the target side are applied in the opposite order as in "snapshot". The
order is now similar to the order in which the snapshots were created on the
source file system: snap1 first, snap2, snap3, snap4, finally "current".

Because this simply reverts parent-child releationship, the efficiency is the
same as for "snapshot". The subvol tree looks different, though: in
particular, the default subvolume ("current") appears to be a read-write
snapshot after cloning, alhough it had no parent in the source file system.

### "generation" strategy

"snapshot" and "choronological" work well for simple linear snapshot
topologies as shown above. But more complex situations are easily possible,
in particular if users create r/w snapshots and perform rollbacks (i.e.
start using a diffent default subvolume, or work on a non-default
subvolume). This results in a tree-like stucture for snapshots.
Consider the following history tree.

Lines denote evolvement of a subvolume in time. Crosses are "forks" (creation
of r/w subvolumes). `*` denotes "static" (ro) subvolumes, `o` non-static (rw)
subvolumes. The btrfs "generation" (transaction ID) increases vertically top-down.

                                      |
                       /--------------+ (5)
                       |              |
            /----------+              G
            |          |
            |          * a
            |          |
            |        3 +--------\
          e o          |        |
                /------+ 1      |
                |      |        |
                |    4 +---\    o b
                |      |   |
                |      |   o c
           /--- + 2    |
           |    |      * d
           |    |      |
         C o    |      |
                |      o M
                |
                o S

We are looking at S. C is a snapshot of S, S is a snapshot of M, M is a
snaphot of G. All other are snapshots of M, like S itself.  IOW: M is "mom" of
S, C a child of S, G an "ancestor" of S, all others are "siblings" of
S. "generation" strategy clones subvols ordered by generation, therefore all
nodes except S have already been cloned when we consider S (but note that we
might have drawn a different picture where e.g. C or M would have higher
generation than S).

It's obvious that the selection of the set of clone sources and the "parent"
for **btrfs-send** is non-trivial.  But this not an unrealistic example if
users work with snapshots and rollbacks.

The "generation" strategy tries to make best guesses for situations like this,
considering the filesystem meta information about generation, generation of
origin, and "is snapshot of" relationship.

If a snapshot existed at node `2`, it would be optimal; next best would be `1,
3, 4`; but these subvolumes might not exist (they exist in a typical
**snapper** topology, unless the user has deleted them, or changed ro
snapshots to rw manually). "static" nodes such as `a` or `d` are preferred
over subvolumes that have changed themselves, such as `b` or `e`. Refer to the
source code for more detail.

### From here onward

The "generation" strategy should work quite well even in complex scenarios.
But there are some situations it can't handle. For example, assume that in the
picture above, there had once been an ro snapshot at node 1, of which S was a
rw snapshot (this is a typical situation for **snapper** rollback. Assume
further that the user had deleted the ro snapshot later on. The `parent_uuid`
link of S would now point to a non-existing subvolume, and the tree with S and
C would be effectively distinct from the rest of the picture. Therefore, the
tool would make two separate copies, and no extent sharing between e.g. S and
M would be possibe, possibly wasting lots of disk space.

The only way to overcome this would be guessing by comparing directory
trees contents, or maybe by applying knowledge about other tools
such as **snapper**, and how they organize subvolumes. I'm unsure if that
would be worth the effort, given that it could also result in wrong guesses.

If someone has a bright idea how to improve on the current strategies,
please step forward!

## The --toplevel option

The toplevel "subvolume" of a BTRFS file system can't be cloned with
send/receive. It's only possible to create a snapshot of the toplevel
FS and clone that. Obviously, the cloned snapshot in the new FS will be
distinct from the toplevel of the new FS. By default, this tool moves the
content of the cloned snapshot to the toplevel of the new FS and deletes
the snapshot. If this is not desired, the `--toplevel` option can be used.
It causes the tool to keep the cloned snapshot volume and create all
subvolumes relative to it.

## Technical note: btrfs-send's -c and -p option

The man page **btrfs-send(8)** says

> It is allowed to omit the -p <parent> option when -c <clone-src> options are
> given, in which case btrfs send will determine a suitable parent among the
> clone sources itself.

`btrfs-send` from btrfs-tools 4.13 selects the parent for a subvolume S and
set of given clone sources C_i [like this][3]:

 1. if `-p` option is specified, use it
 2. if S has no `parent_uuid` set, or this uuid can't be found, give up
 3. if there's `C_i` with `C_i->uuid == S->parent_uuid` (the subvolume of
    which S is a child (snapshot), let's call it "mom"), use it
 4. if no C_i has the same `parent_uuid` as S, give up
 5. from all C_i that are children of "mom", choose the one that has the
    closest generation (actually, `ctransid`, what exactly is the difference
    to "generation"?) to "mom".

Note that [the wiki][2] is a bit misleading, because it suggests that `-c`
without `p` is different from `-c` with `-p`, although `-p` is usually implied by
the algorithm above. The only relevant exception is sending subvolumes that
have no parent.

[1]: https://superuser.com/questions/607363/how-to-copy-a-btrfs-filesystem
[2]: https://btrfs.wiki.kernel.org/index.php/FAQ#What_is_the_difference_between_-c_and_-p_in_send.3F
[3]: https://git.kernel.org/pub/scm/linux/kernel/git/kdave/btrfs-progs.git/tree/cmds-send.c
[4]: https://lyte.id.au/2012/03/21/moving-my-butter/
