"""Exercise real Bash transition control flow with device operations replaced."""

import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]

TRANSITION = r'''
VM=test-vm
ALLOW_GUI=1
READY_DELAY=0
GPU_PCI=(0000:01:00.0)
USB_DEVICES=('Missing keyboard|1234|abcd')
printf 'shut off' >"$PROBE_DIR/state"
printf active >"$PROBE_DIR/display"
event() { printf '%s\n' "$*" >>"$PROBE_DIR/events"; }
load_runtime_config() { :; }
check_pci_devices() { :; }
prepare_usb_startup() { event optional-policy; }
begin_root_session() { :; }
as_root() { "$@"; }
usb_present() { return 1; }
domstate() { cat "$PROBE_DIR/state"; }
driver_for() {
  if [[ "$(domstate)" == running ]]; then printf vfio-pci; else printf nvidia; fi
}
active_gpu_users() { :; }
verify_nvidia() { event health; }
assert_pci_state_sane() { :; }
unload_gpu_modules() { event unload; }
load_gpu_modules() { event reload; }
sleep() { SECONDS=$((SECONDS + 1)); }
systemctl() {
  case "$1" in
    is-active) [[ "$(cat "$PROBE_DIR/display")" == active ]] ;;
    stop) event desktop-stop; printf inactive >"$PROBE_DIR/display" ;;
    start) event desktop-start; printf active >"$PROBE_DIR/display" ;;
    *) return 99 ;;
  esac
}
virsh() {
  case "$3" in
    start) event vm-start; printf running >"$PROBE_DIR/state" ;;
    shutdown) event vm-shutdown; printf 'shut off' >"$PROBE_DIR/state" ;;
    *) event "unexpected virsh: $*"; return 99 ;;
  esac
}
'''


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vm-helper-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = dict(os.environ, PROBE_DIR=self.temp.name, XDG_RUNTIME_DIR=self.temp.name)

    def bash(self, script, expected=0):
        result = subprocess.run(
            ["bash", "-c", 'VM_MANAGER_CONFIG=/nonexistent source ./vm-helper\n' + script],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def events(self):
        return (self.directory / "events").read_text().splitlines()

    def test_cold_coexist_finishes_without_keyboard(self):
        self.bash(TRANSITION + 'start_coexist_mode\n')
        events = self.events()
        self.assertEqual((self.directory / "state").read_text(), "running")
        self.assertEqual((self.directory / "display").read_text(), "active")
        self.assertLess(events.index("optional-policy"), events.index("desktop-stop"))
        self.assertLess(events.index("unload"), events.index("vm-start"))
        self.assertLess(events.index("vm-start"), events.index("desktop-start"))

    def test_linux_shutdown_finishes_in_one_invocation(self):
        self.bash(TRANSITION + 'printf running >"$PROBE_DIR/state"\nreturn_linux_mode\n')
        self.assertEqual((self.directory / "state").read_text(), "shut off")
        self.assertEqual(self.events(), ["vm-shutdown", "reload", "health", "desktop-start"])

    def test_windows_retry_does_not_start_vm_twice(self):
        self.bash(TRANSITION + 'start_windows_mode\nstart_windows_mode\n')
        self.assertEqual(self.events().count("vm-start"), 1)
        self.assertEqual((self.directory / "display").read_text(), "inactive")

    def test_locked_action_and_classic_menu_keep_errexit(self):
        script = 'probe() { false; printf CONTINUED; }\nwith_mutation_lock probe\n'
        self.assertNotIn("CONTINUED", self.bash(script, expected=1).stdout)
        script = '_start_windows_mode() { false; printf CONTINUED; }\nrun_menu_action start_windows_mode\n'
        result = self.bash(script)
        self.assertNotIn("CONTINUED", result.stdout)
        self.assertIn("action failed", result.stderr)

    def test_failed_disk_inspection_is_fatal_even_in_a_conditional(self):
        for failure in ("state", "mounts"):
            with self.subTest(failure=failure):
                script = r'''
VM_RAW_DISKS=(/dev/fake)
resolve_raw_disk() { printf /dev/fake; }
lsblk() {
  if [[ "$1" == -dnro ]]; then
    if [[ "$FAILURE" == state ]]; then return 1; fi
    printf live
  else
    return 1
  fi
}
if with_mutation_lock check_raw_disks; then printf UNSAFE_SUCCESS; fi
'''
                result = self.bash(f'FAILURE={failure}\n' + script, expected=1)
                self.assertNotIn("UNSAFE_SUCCESS", result.stdout)
                self.assertIn(f"could not inspect raw disk {failure}", result.stderr)

    def test_disk_offline_and_mounts_still_block_start(self):
        for state, mount in (("offline", ""), ("live", "/dev/fake1 /mnt/windows")):
            with self.subTest(state=state, mount=mount):
                self.env.update(DISK_STATE=state, DISK_MOUNT=mount)
                self.bash(r'''
VM_RAW_DISKS=(/dev/fake)
resolve_raw_disk() { printf /dev/fake; }
lsblk() {
  if [[ "$1" == -dnro ]]; then printf '%s\n' "$DISK_STATE"
  else printf '%s\n' "$DISK_MOUNT"; fi
}
with_mutation_lock check_raw_disks
''', expected=1)

    def test_raw_disk_resolver_rejects_regular_files_and_missing_paths(self):
        (self.directory / "regular").touch()
        for name in ("regular", "missing"):
            self.bash(f'resolve_raw_disk "$PROBE_DIR/{name}"\n', expected=1)

    def test_drm_scan_finds_full_domain_bdf_paths(self):
        (self.directory / "card").touch()
        (self.directory / "pci-0000:01:00.0-card").symlink_to("card")
        result = self.bash(r'''
GPU_PCI=(0000:01:00.0)
dri_by_path_dir() { printf '%s' "$PROBE_DIR"; }
read_file() { printf 0x1002; }
as_root() { [[ "$1" == fuser ]] || exit 99; printf 12345; }
active_gpu_users
''')
        self.assertIn("12345", result.stdout)
        self.assertIn("pci-0000:01:00.0-card", result.stdout)

    def test_gpu_release_waits_and_times_out_without_starting_vm(self):
        result = self.bash(TRANSITION + r'''
GPU_RELEASE_TIMEOUT=2
active_gpu_users() { printf '1234 busy-client'; }
start_windows_mode
''', expected=1)
        self.assertNotIn("vm-start", self.events())
        self.assertIn("1234 busy-client", result.stderr)
        self.assertIn("after 2s", result.stderr)

    def test_gpu_release_can_complete_after_a_delay(self):
        self.bash(TRANSITION + r'''
active_gpu_users() { [[ -e "$PROBE_DIR/released" ]] || printf '1234 client'; }
sleep() { touch "$PROBE_DIR/released"; SECONDS=$((SECONDS + 1)); }
start_windows_mode
''')
        self.assertIn("vm-start", self.events())

    def test_module_unload_retries_without_ignoring_persistent_failure(self):
        script = r'''
GPU_RELEASE_TIMEOUT=2
selected_gpu_is_nvidia() { return 0; }
warn_if_nvidia_modules_updated_since_boot() { :; }
nvidia_driver_has_other_gpu() { return 1; }
gpu_module_loaded() { [[ "$1" == nvidia ]]; }
sleep() { SECONDS=$((SECONDS + 1)); }
as_root() {
  if [[ "$UNLOAD_FAILS" == always || ! -e "$PROBE_DIR/attempt" ]]; then
    touch "$PROBE_DIR/attempt"; printf 'Module is in use'; return 1
  fi
}
unload_gpu_modules
'''
        self.bash('UNLOAD_FAILS=once\n' + script)
        result = self.bash('UNLOAD_FAILS=always\n' + script, expected=1)
        self.assertIn("failed to unload nvidia within 2s", result.stderr)

    def test_optional_usb_attach_failure_does_not_prevent_coexist(self):
        self.bash(TRANSITION + r'''
usb_present() { return 0; }
usb_device_count() { printf 1; }
usb_id_is_hub() { return 1; }
usb_live_count() { printf 0; }
eval "$(declare -f virsh | sed '1s/virsh/transition_virsh/')"
virsh() {
  if [[ "$3" == attach-device ]]; then event failed-usb-attach; return 1; fi
  transition_virsh "$@"
}
start_coexist_mode
''')
        self.assertIn("failed-usb-attach", self.events())
        self.assertEqual((self.directory / "display").read_text(), "active")

    def test_optional_usb_policy_preserves_other_xml_and_namespaces(self):
        xml = '''<domain type="kvm" xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0">
<name>fixture</name><uuid>1234</uuid><os><nvram>/private/vm.fd</nvram></os>
<devices><disk type="block"><source dev="/dev/raw"/></disk>
<hostdev type="pci" managed="yes"><source><address bus="0x01"/></source></hostdev>
<hostdev type="usb"><source guestReset="off"><vendor id="0x1234"/><product id="0xabcd"/>
<address bus="1" device="2"/></source><boot order="2"/></hostdev></devices>
<qemu:commandline><qemu:arg value="preserve-me"/></qemu:commandline></domain>'''
        result = subprocess.run(["python3", "scripts/optional-usb.py"], input=xml,
                                text=True, capture_output=True, cwd=ROOT, check=True)
        actual = ET.fromstring(result.stdout)
        source = actual.find('./devices/hostdev[@type="usb"]/source')
        self.assertEqual(source.attrib, {"guestReset": "off", "startupPolicy": "optional"})
        del source.attrib["startupPolicy"]
        self.assertEqual(ET.tostring(actual), ET.tostring(ET.fromstring(xml)))

    def test_usb_prepare_saves_backup_before_definition_and_is_idempotent(self):
        xml = '<domain><name>test</name><devices><hostdev type="usb"><source><vendor id="0x1234"/><product id="0xabcd"/></source></hostdev></devices></domain>'
        (self.directory / "domain.xml").write_text(xml)
        self.bash(r'''
CONFIG_FILE="$PROBE_DIR/vm-helper.env"
VM=test
VM_XML="$(cat "$PROBE_DIR/domain.xml")"
required_domstate() { printf 'shut off'; }
virsh() {
  case "$3" in
    dumpxml) cat "$PROBE_DIR/domain.xml" ;;
    define)
      compgen -G "$PROBE_DIR/vm-helper.env.bak.domain-*" >/dev/null || exit 99
      printf define >>"$PROBE_DIR/defines"
      cp "$4" "$PROBE_DIR/domain.xml" ;;
    *) exit 99 ;;
  esac
}
with_mutation_lock prepare_usb_startup
with_mutation_lock prepare_usb_startup
''')
        self.assertEqual((self.directory / "defines").read_text(), "define")
        backups = list(self.directory.glob("vm-helper.env.bak.domain-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text().strip(), xml)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_pci_discovery_includes_wifi_without_treating_it_as_gpu(self):
        result = self.bash(r'''
VM_XML='<domain><devices>
<hostdev type="pci" managed="yes"><source><address domain="0x0000" bus="0x01" slot="0x00" function="0x0"/></source></hostdev>
<hostdev type="pci" managed="yes"><source><address domain="0x0000" bus="0x09" slot="0x00" function="0x0"/></source></hostdev>
</devices></domain>'
read_file() { if [[ "$1" == *01:00.0* ]]; then printf 0x030000; else printf 0x028000; fi; }
discover_pci_from_vm
discover_gpu_from_vm
printf 'all=%s gpu=%s\n' "${VM_PCI_DEVICES[*]}" "${GPU_PCI[*]}"
''')
        self.assertIn("all=0000:01:00.0 0000:09:00.0 gpu=0000:01:00.0", result.stdout)

    def test_wifi_group_reports_host_nic_but_not_bridge(self):
        result = self.bash(r'''
VM_PCI_DEVICES=(0000:09:00.0)
pci_group_devices() { printf '%s\n' 0000:09:00.0 0000:0c:00.0 0000:04:08.0; }
read_file() { if [[ "$1" == *04:08.0* ]]; then printf 0x060400; else printf 0x020000; fi; }
driver_for() { printf r8169; }
pci_label() { printf 'Host Ethernet'; }
pci_group_conflicts 0000:09:00.0
''')
        self.assertIn("0000:0c:00.0 driver=r8169", result.stdout)
        self.assertNotIn("0000:04:08.0", result.stdout)

    def test_gpu_queries_skip_active_transition(self):
        runtime = self.directory / "vm-helper"
        runtime.mkdir(mode=0o700)
        with (runtime / "gpu-query.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.bash('query() { printf UNSAFE_QUERY; }\nwith_gpu_read_lock query\n', expected=75)
            self.assertNotIn("UNSAFE_QUERY", result.stdout)
            result = self.bash('_verify_nvidia() { printf UNSAFE_QUERY; }\nverify_nvidia\n', expected=1)
            self.assertIn("health check unavailable", result.stderr)
            self.assertNotIn("UNSAFE_QUERY", result.stdout)

    def test_transition_waits_for_existing_query(self):
        runtime = self.directory / "vm-helper"
        runtime.mkdir(mode=0o700)
        with (runtime / "gpu-query.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            process = subprocess.Popen(
                ["bash", "-c", 'VM_MANAGER_CONFIG=/nonexistent source ./vm-helper\n'
                 'action() { printf FINISHED; }\nwith_mutation_lock action\n'],
                cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.15)
                fcntl.flock(lock, fcntl.LOCK_UN)
                stdout, stderr = process.communicate(timeout=3)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn(b"FINISHED", stdout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


    def test_unmanaged_pci_cannot_pass_membership_check(self):
        for managed, expected in (("yes", 0), ("no", 1)):
            self.bash(f"""
VM_XML='<domain><devices><hostdev type="pci" managed="{managed}"><source><address domain="0x0000" bus="0x01" slot="0x00" function="0x0"/></source></hostdev></devices></domain>'
vm_has_pci_hostdev 0000:01:00.0
""", expected=expected)

    def test_failed_preflight_stops_before_desktop_or_vm_mutation(self):
        result = self.bash(TRANSITION + r"""
check_raw_disks() { die "fixture disk blocked"; }
start_windows_mode
""", expected=1)
        self.assertIn("fixture disk blocked", result.stderr)
        self.assertFalse((self.directory / "events").exists())

    def test_start_timeout_does_not_start_host_desktop(self):
        result = self.bash(TRANSITION + r"""
virsh() { event vm-start; printf paused >"$PROBE_DIR/state"; }
start_coexist_mode
""", expected=1)
        self.assertIn("did not reach running state", result.stderr)
        self.assertNotIn("desktop-start", self.events())

    def test_vfio_ownership_blocks_desktop_after_guest_shutdown(self):
        result = self.bash(TRANSITION + r"""
printf running >"$PROBE_DIR/state"
driver_for() { printf vfio-pci; }
reattach_if_needed() { event reattach-failed; }
return_linux_mode
""", expected=1)
        self.assertIn("refusing to start the desktop", result.stderr)
        self.assertNotIn("desktop-start", self.events())

    def test_state_query_error_ends_wait(self):
        self.bash('domstate() { return 1; }\nwait_for_state running 30\n', expected=1)

    def test_changed_domain_definition_is_never_overwritten(self):
        result = self.bash(r"""
CONFIG_FILE="$PROBE_DIR/vm-helper.env"
VM=test
VM_XML='<domain><devices><hostdev type="usb"><source/></hostdev></devices></domain>'
required_domstate() { printf 'shut off'; }
virsh() {
  if [[ "$3" == dumpxml ]]; then printf '<domain><name>changed</name></domain>'
  else printf UNSAFE_DEFINE; fi
}
with_mutation_lock prepare_usb_startup
""", expected=1)
        self.assertIn("definition changed", result.stderr)
        self.assertNotIn("UNSAFE_DEFINE", result.stdout)
        self.assertEqual(list(self.directory.glob("vm-helper.env.bak.domain-*")), [])


if __name__ == "__main__":
    unittest.main()
