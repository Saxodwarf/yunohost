#!/usr/bin/env python

import os
import dns.resolver
import socket
import re

from subprocess import CalledProcessError
from types import FunctionType

from moulinette.utils.process import check_output
from moulinette.utils.network import download_text
from moulinette.utils.filesystem import read_yaml

from yunohost.diagnosis import Diagnoser
from yunohost.domain import _get_maindomain, domain_list
from yunohost.utils.error import YunohostError

DIAGNOSIS_SERVER = "diagnosis.yunohost.org"

DEFAULT_DNS_BLACKLIST = "/usr/share/yunohost/other/dnsbl_list.yml"


class MailDiagnoser(Diagnoser):

    id_ = os.path.splitext(os.path.basename(__file__))[0].split("-")[1]
    cache_duration = 600
    dependencies = ["ip"]

    def run(self):

        self.ehlo_domain = _get_maindomain()
        self.mail_domains = domain_list()["domains"]
        self.ipversions, self.ips = self.get_ips_checked()

        # TODO Is a A/AAAA and MX Record ?
        # TODO Are outgoing public IPs authorized to send mail by SPF ?
        # TODO Validate DKIM and dmarc ?
        # TODO check that the recent mail logs are not filled with thousand of email sending (unusual number of mail sent)
        # TODO check for unusual failed sending attempt being refused in the logs ?
        checks = [name for name, value in MailDiagnoser.__dict__.items()
                       if type(value) == FunctionType and name.startswith("check_")]
        for check in checks:
            self.logger_debug("Running " + check)
            for report in getattr(self, check):
                yield report
            else:
                name = checks[6:]
                yield dict(meta={"test": "mail_" + name},
                        status="SUCCESS",
                        summary="diagnosis_mail_" + name + "_ok")


    def check_outgoing_port_25(self):
        """
        Check outgoing port 25 is open and not blocked by router
        This check is ran on IPs we could used to send mail.
        """

        for ipversion in self.ipversions:
            cmd = '/bin/nc -{ipversion} -z -w2 yunohost.org 25'.format({
                  'ipversion': ipversion})
            if os.system(cmd) != 0:
                yield dict(meta={"test": "outgoing_port_25", "ipversion": ipversion},
                           data={},
                           status="ERROR",
                           summary="diagnosis_mail_ougoing_port_25_blocked")


    def check_ehlo(self):
        """
        Check the server is reachable from outside and it's the good one
        This check is ran on IPs we could used to send mail.
        """

        for ipversion in self.ipversions:
            try:
                r = Diagnoser.remote_diagnosis('check-smtp',
                                               data={},
                                               ipversion=ipversion)
            except Exception as e:
                yield dict(meta={"test": "mail_ehlo", "ipversion": ipversion},
                           data={"error": e},
                           status="WARNING",
                           summary="diagnosis_mail_ehlo_could_not_diagnose")
                continue

            if r["status"] == "error_smtp_unreachable":
                yield dict(meta={"test": "mail_ehlo", "ipversion": ipversion},
                           data={},
                           status="ERROR",
                           summary="diagnosis_mail_ehlo_unavailable")
            elif r["helo"] != self.ehlo_domain:
                yield dict(meta={"test": "mail_ehlo", "ipversion": ipversion},
                           data={"wrong_ehlo": r["helo"], "right_ehlo": self.ehlo_domain},
                           status="ERROR",
                           summary="diagnosis_mail_ehlo_wrong")


    def check_fcrdns(self):
        """
        Check the reverse DNS is well defined by doing a Forward-confirmed
        reverse DNS check
        This check is ran on IPs we could used to send mail.
        """

        for ip in self.ips:
            try:
                rdns_domain, _, _ = socket.gethostbyaddr(ip)
            except socket.herror:
                yield dict(meta={"test": "mail_fcrdns", "ip": ip},
                           data={"ehlo_domain": self.ehlo_domain},
                           status="ERROR",
                           summary="diagnosis_mail_reverse_dns_missing")
                continue
            if rdns_domain != self.ehlo_domain:
                yield dict(meta={"test": "mail_fcrdns", "ip": ip},
                           data={"ehlo_domain": self.ehlo_domain,
                                 "rdns_domain": rdns_domain},
                           status="ERROR",
                           summary="diagnosis_mail_rdns_different_from_ehlo_domain")


    def check_blacklist(self):
        """
        Check with dig onto blacklist DNS server
        This check is ran on IPs and domains we could used to send mail.
        """

        dns_blacklists = read_yaml(DEFAULT_DNS_BLACKLIST)
        for item in self.ips + self.mail_domains:
            for blacklist in dns_blacklists:
                item_type = "domain"
                if ":" in item:
                    item_type = 'ipv6'
                elif re.match(r'^\d+\.\d+\.\d+\.\d+$', item):
                    item_type = 'ipv4'

                if not blacklist[item_type]:
                    continue

                # Determine if we are listed on this RBL
                try:
                    subdomain = item
                    if item_type != "domain":
                        rev = dns.reversename.from_address(item)
                        subdomain = str(rev.split(3)[0])
                    query = subdomain + '.' + blacklist['dns_server']
                    # TODO add timeout lifetime
                    dns.resolver.query(query, "A")
                except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.resolver.NoAnswer,
                dns.exception.Timeout):
                    continue

                # Try to get the reason
                try:
                    reason = str(dns.resolver.query(query, "TXT")[0])
                except Exception:
                    reason = "-"

                yield dict(meta={"test": "mail_blacklist", "item": item,
                                 "blacklist": blacklist["dns_server"]},
                           data={'blacklist_name': blacklist['name'],
                                 'blacklist_website': blacklist['website'],
                                 'reason': reason},
                           status="ERROR",
                           summary='diagnosis_mail_blacklist_listed_by')

    def check_queue(self):
        """
        Check mail queue is not filled with hundreds of email pending
        """

        command = 'postqueue -p | grep -v "Mail queue is empty" | grep -c "^[A-Z0-9]"'
        try:
            output = check_output(command).strip()
            pending_emails = int(output)
        except (ValueError, CalledProcessError) as e:
            yield dict(meta={"test": "mail_queue"},
                       data={"error": e},
                       status="ERROR",
                       summary="diagnosis_mail_cannot_get_queue")
        else:
            if pending_emails > 100:
                yield dict(meta={"test": "mail_queue"},
                           data={'nb_pending': pending_emails},
                       status="WARNING",
                       summary="diagnosis_mail_queue_too_many_pending_emails")
            else:
                yield dict(meta={"test": "mail_queue"},
                           data={'nb_pending': pending_emails},
                           status="SUCCESS",
                           summary="diagnosis_mail_queue_ok")


    def get_ips_checked(self):
        outgoing_ipversions = []
        outgoing_ips = []
        ipv4 = Diagnoser.get_cached_report("ip", {"test": "ipv4"}) or {}
        if ipv4.get("status") == "SUCCESS":
            outgoing_ipversions.append(4)
            global_ipv4 = ipv4.get("data", {}).get("global", {})
            if global_ipv4:
                outgoing_ips.append(global_ipv4)

        ipv6 = Diagnoser.get_cached_report("ip", {"test": "ipv6"}) or {}
        if ipv6.get("status") == "SUCCESS":
            outgoing_ipversions.append(6)
            global_ipv6 = ipv6.get("data", {}).get("global", {})
            if global_ipv6:
                outgoing_ips.append(global_ipv6)
        return (outgoing_ipversions, outgoing_ips)

def main(args, env, loggers):
    return MailDiagnoser(args, env, loggers).diagnose()
