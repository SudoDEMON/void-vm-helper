#!/usr/bin/env python3
"""Make persistent USB host devices optional, preserving other domain XML."""

import sys
from xml.dom import minidom


def optional_usb_xml(xml: str) -> str:
    document = minidom.parseString(xml)
    domain = document.documentElement
    if domain.tagName != "domain":
        raise ValueError("expected a libvirt domain")
    for devices in domain.childNodes:
        if devices.nodeType != devices.ELEMENT_NODE or devices.tagName != "devices":
            continue
        for hostdev in devices.childNodes:
            if (hostdev.nodeType != hostdev.ELEMENT_NODE or hostdev.tagName != "hostdev"
                    or hostdev.getAttribute("type") != "usb"):
                continue
            for source in hostdev.childNodes:
                if source.nodeType == source.ELEMENT_NODE and source.tagName == "source":
                    source.setAttribute("startupPolicy", "optional")
    return domain.toxml()


if __name__ == "__main__":
    sys.stdout.write(optional_usb_xml(sys.stdin.read()) + "\n")
