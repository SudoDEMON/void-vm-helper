# Limine and bare-metal Windows boot order

Limine is the Linux bootloader. Motherboard UEFI boot entries and the menu inside Limine are separate lists. Starting Windows from Limine still runs Windows on the physical motherboard.

Windows installation, repair, or an upgrade can register Windows Boot Manager and move it to the front of the motherboard's boot order. Microsoft documents this as BCDBoot's default behavior. For a deliberate BCDBoot repair, `/p` preserves the position of an existing Windows entry and `/addlast` requests adding it last; these options are not a permanent firmware policy. See [Microsoft's BCDBoot documentation](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/bcdboot-command-line-options-techref-di?view=windows-11).

The VM's OVMF NVRAM file is separate from motherboard NVRAM. Passing a whole NVMe disk lets the guest change files on that disk, including its EFI System Partition. It does not give the guest access to the motherboard's firmware variables. Bare-metal Windows can access those variables directly.

From Linux, inspect the current order with:

```bash
efibootmgr -v
```

Find the four-digit entry named `Limine`, then restore it to the front with `sudo efibootmgr --bootorder` followed by the complete comma-separated list of current IDs. Preserve the remaining entries and their order. For example, **only if the current IDs are Limine `0000`, UEFI OS `0002`, and Windows `0001`**, use:

```bash
sudo efibootmgr --bootorder 0000,0002,0001
```

Confirm with `efibootmgr -v` afterward. This changes the order; it does not reactivate a disabled entry or prevent a future Windows or firmware update from changing the order again. If Linux is skipped, use the motherboard's one-time boot menu to reach Limine first.

The helper's VM transitions do not change motherboard boot order, delete boot entries, replace Windows EFI executables, or install an automatic boot-order service.
