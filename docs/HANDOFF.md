# Handoff Notes

This repository has one safety-critical behavioral source: the Bash `vm-helper` backend. The former `vm-gpu-manager` name and all legacy commands are symlinks into it. The standard-library `vm_helper_tui` package renders and collects telemetry around versioned backend records; it must not grow independent device-transition logic.

## First Run On Another Host

1. Enable IOMMU support in firmware and confirm the intended GPU has usable isolation.
2. Install libvirt/QEMU, `virsh`, `xmllint`, PCI/USB utilities, and the host GPU driver.
3. Define the VM and add the GPU functions as persistent managed PCI host devices.
4. Run `./vm-helper hardware` to inspect CPU, GPUs, drivers, IOMMU groups, disks, and USB.
5. Run `./vm-helper configure` to select the VM, GPU, and USB devices and review companions, persistence, pinning, timers, and attached raw disks.
6. Review `./vm-helper status` before the first transition.

When libvirt has exactly one domain with display-class PCI hostdevs, the tool can derive VM, GPU functions, USB hostdevs, and raw disk directly from inactive XML without a config file.

## Configuration Contract

The local `vm-helper.env` is Bash syntax and intentionally ignored. The wizard creates it with mode `0600` and backs up an existing copy. Supported values are:

- `VM`, `URI`: libvirt domain and connection.
- `WIN_DISK`: optional stable expected raw block path; when set, it must resolve to a raw disk already attached in inactive VM XML. Every attached raw disk is checked regardless.
- `GPU_PCI`: selected GPU/companion PCI functions.
- `GPU_MODULES`: modules needed when returning the GPU to Linux.
- `USB_DEVICES`: optional startup peripherals as `Label|vendor|product` entries. Missing mouse/keyboard devices must not block startup.
- `USB_AUTO_DISCOVER`: discover USB hostdevs from inactive VM XML when the array is empty.
- transition timing and `ALLOW_GUI` values documented in `vm-helper.env.example`.

Legacy `REQUIRED_USB` is mapped to optional `USB_DEVICES`. `GPU_NODE` is no longer needed because libvirt node names are derived from PCI addresses. `GPU_RELEASE_TIMEOUT` bounds GPU-user waits and NVIDIA unload retries (30 seconds by default).

The validated internal configuration endpoint accepts exactly one VM, one display-class GPU BDF, and repeated connected USB VID:PID values. It derives same-slot IOMMU companions and host modules itself, then uses the same locked, backed-up mode-`0600` writer as the classic wizard.

## TUI Architecture

- `vm_helper_tui/app.py`: non-blocking refresh scheduling, confirmations, sudo handoff, detached actions, and wizard orchestration.
- `vm_helper_tui/state.py`: panel registry/default layout profile, responsive thresholds, input/mouse mapping, and wizard state.
- `vm_helper_tui/views.py`: curses rendering with ASCII borders and adaptive color pairs.
- `vm_helper_tui/protocol.py`: strict `VMH1` NUL-record parser, synchronous action/configuration calls, pollable read-only telemetry calls, and action/log reconnection.
- `vm_helper_tui/telemetry.py`: `/proc` host collection, counter deltas, mode derivation, and 60-sample sparklines.

`vm-helper machine snapshot` and `machine inventory [VM]` emit a `VMH1\0` header followed by typed records, decimal field counts, and NUL-terminated fields. Never replace this with parsing the human `status` or `hardware` output. Additive record types are compatible; changing field meaning requires a protocol version bump and parser tests.

The panel registry and default profile intentionally make layout order/visibility data-driven, but v1 exposes no customization UI or persistent layout file. Dashboard actions never replace the dashboard with a terminal view: detached output remains in Activity Log, while USB and Configure are explicit full pages within the same curses session.

The live USB page consumes additive `usb_route` inventory records. Each record includes label, VID, PID, presence, default-configuration membership, live owner, and physical match count. The page stages Linux/Windows targets and submits one `usb-route` worker with repeated validated `--linux VID:PID` or `--windows VID:PID` pairs. Direct bulk `usb attach` and `usb detach` commands remain compatible.

## Operational Invariants

- A selected GPU PCI function must exist both on the host and as a persistent VM PCI hostdev.
- Every attached raw block disk must be online and completely unmounted on Linux.
- Persistent USB sources are made optional before VM startup, with an original XML backup. Missing USB and optional hotplug failures must not stop a successful GPU/VM transition. `scripts/optional-usb.py` only transforms XML; Bash owns backup, validation, and libvirt operations.
- The display manager is stopped before handing the GPU to the VM.
- TUI sudo authorization is acquired on the originating TTY. A user-owned, SIGHUP-resistant supervisor must invoke `sudo -n` before `setsid`; the resulting detached worker runs privileged and never prompts after the terminal or USB keyboard is gone. Never reverse that ordering.
- Active GPU users and busy NVIDIA modules stop the transition.
- Post-start verification requires every selected and persistent PCI function on `vfio-pci`, including non-GPU devices such as Wi-Fi adapters.
- Return-to-Linux skips libvirt reattach for devices already on a host driver. This prevents the fresh-boot reattach bug that can leave NVIDIA half-detached.
- Return-to-Linux refuses to start the display manager while any selected function remains on `vfio-pci`.
- The display manager starts only after the returned GPU has a host driver and vendor health checks pass.
- Guest shutdown remains graceful; no helper uses `virsh destroy`.
- All GPU, USB, and configuration mutations enter the same `flock`, including nested coexist/USB paths.
- Keep locked callbacks outside `if`, `!`, and conditional lists: those contexts disable Bash errexit throughout the callback. Safety-critical disk inspection also explicitly rejects failures, even when invoked from a conditional by a caller.
- A separate `gpu-query.lock` drains existing NVIDIA queries before a mutation and excludes new ones during the transition. The unprivileged launcher creates both lock files before a root worker can use them, preserving user access. Exit cleanup explicitly unlocks inherited descriptors.
- Per-device USB batches reject missing devices, duplicate VID:PID assignments, Linux root hubs, and ambiguous identical devices before the first mutation. Operational failures may leave earlier requested routes applied; log that partial result and do not add automatic rollback.
- TUI privileged workers are detached session leaders watched by unprivileged supervisors. Supervisors atomically maintain user-owned binary metadata and text logs below `$XDG_RUNTIME_DIR/vm-helper/` with mode `0600`; closing curses must never signal or cancel them.

## Vendor Behavior

NVIDIA module handling is explicit because its DRM/UVM/modeset stack must be unloaded in dependency order before passthrough and reloaded in dependency order afterward. AMD and Intel host modules are inferred for return-to-Linux, but they are not globally unloaded before VM start because the same module may also own the host iGPU. For those vendors, libvirt performs per-device detach and reports a busy device normally.

## Troubleshooting

### Wi-Fi and Bluetooth

PCI Wi-Fi is supported as an additional persistent managed PCI hostdev in the VM definition. Keep `GPU_PCI` limited to the selected GPU and its companions. The helper discovers other persistent PCI devices, checks their complete IOMMU groups before stopping the desktop, verifies VFIO ownership after startup, and reattaches them on return to Linux.

A Wi-Fi adapter sharing its IOMMU group with host-owned Ethernet, storage, or USB controllers cannot be handed over independently. A down or unused Wi-Fi interface does not resolve that isolation constraint. Inspect the group with `ls -l /sys/bus/pci/devices/<BDF>/iommu_group/devices/`. Do not detach the host's other devices to work around the check.

A USB Bluetooth adapter can use the ordinary USB configuration and routing pages once Linux enumerates it. An absent adapter cannot be selected or passed through; the USB label of a wireless keyboard does not identify a Bluetooth controller.

### Startup and recovery

Windows and Linux transitions poll VM state with a separately initialized deadline. Cover actual cold startup and graceful shutdown paths in regression tests; testing only already-running coexist mode will miss failures between the libvirt operation and desktop restoration.

When a USB policy backup is needed, it is saved next to `VM_MANAGER_CONFIG` as `<config>.bak.domain-*`. Restore a reviewed backup with `virsh -c <URI> define <backup> --validate` while the VM is stopped. Future helper starts will make USB optional again.

Use the read-only report first:

```bash
./vm-helper status
./vm-helper hardware
```

If PCI `uevent` names a driver but the device has no `driver` symlink, the device is partially transitioned. Reboot before another ownership command.

If an NVIDIA module cannot reload after a rolling-release update, compare the running kernel with `modinfo -F vermagic nvidia`; the tool prints these diagnostics on failure. A reboot into the matching kernel/module set is the normal recovery.

If VM startup reports a selected PCI function is not persistent, add that function to the inactive libvirt domain as a managed PCI hostdev before retrying.

If USB attachment is ambiguous because multiple identical VID:PID devices are connected, the live page shows the duplicate count and disables routing. Use persistent VM XML with explicit USB source addressing or extend the local configuration model before relying on live VID:PID attachment.

If the TUI cannot initialize, use `classic-menu`, `configure-classic`, or `VM_HELPER_TUI=0`. `TERM=dumb` bypasses curses automatically. A raw Linux console should use `TERM=linux`; mouse reporting is optional there.

If a transition outlives its terminal, relaunch the dashboard to reconnect to the newest active action. A stale metadata record is not treated as an active lock: the Bash `flock` remains the authority. Never add force-kill UI for transition workers.

Dynamic snapshots and static inventory run as separate pollable subprocess groups; they must never block `getch()`. Dashboard exit may terminate those read-only collectors, but it must not signal detached transition workers. Starting an action keeps the menu active and streams the same reconnectable log into Activity Log. Metadata state plus return code must remain visible after its PID exits.

## Validation Additions

`tests/vm-helper-test.sh` covers binary protocol framing, internal argument validation, per-device USB safety, shared mutation locking, sudo-before-`setsid` worker supervision, user-owned completion metadata, and direct-command dispatch. `tests/test_backend_transitions.py` exercises real transition control flow with mocked device operations: cold starts, graceful shutdown, missing/failed USB, disk inspection failures, DRM user detection, bounded release/unload waits, PCI discovery/isolation, XML backups, and concurrent GPU query locking. `tests/test_tui_unit.py` covers parsing, deltas, sparklines, modes, layouts, input/mouse mapping, wizard/USB page state, non-blocking calls, outcome labels, and detached-log reconnection. `tests/test_tui_pty.py` exercises `TERM=linux` at 80x24, `xterm-256color` at 140x40, resize/redraw/navigation, dashboard Activity Log streaming, staged USB routing, slow-telemetry input responsiveness, classic fallback, clean exit, and terminal restoration. `tests/fake-vm-helper` never touches real devices.

## Repository Hygiene

Do not commit `vm-helper.env` or its backups, evidence, VM images, ISOs, OVMF variables, TPM state, logs, project memory, project-specific agent instructions, or authentication/session data. The public repository should contain only the generic tool, symlink entrypoints, example config, and documentation.
