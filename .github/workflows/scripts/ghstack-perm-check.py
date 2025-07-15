#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Literal, Tuple

import requests


def check_blocking_statuses(statuses: List[Dict[str, Any]]) -> Tuple[Literal["pending", "failed", "success"], List[str]]:
    land_blocking_statuses = [
        # Fill in your own required tests here!
    ]
    failed_statuses = []
    pending_statuses = []
    for status in statuses:
        if status["context"] in land_blocking_statuses:
            if status["state"] == "failure":
                failed_statuses.append(status["context"])
            elif status["state"] == "pending":
                pending_statuses.append(status["context"])
    if failed_statuses:
        return "failed", failed_statuses
    elif pending_statuses:
        return "pending", pending_statuses
    else:
        return "success", []

def main():
    parser = argparse.ArgumentParser(description="Check ghstack PR permissions and status")
    parser.add_argument("pr_number", type=int, help="PR number to check")
    parser.add_argument("head_ref", help="Head reference of the PR")
    parser.add_argument("repo", help="Repository in owner/repo format")
    parser.add_argument("--max-wait-time", type=int, default=1800, help="Maximum wait time in seconds for PR status to change from unstable")

    args = parser.parse_args()

    gh = requests.Session()
    gh.headers.update(
        {
            "Authorization": f'Bearer {os.environ["GITHUB_TOKEN"]}',
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    NUMBER, head_ref, REPO = args.pr_number, args.head_ref, args.repo
    MAX_WAIT_TIME = args.max_wait_time

    def must(cond, msg):
        if not cond:
            print(msg)
            gh.post(
                f"https://api.github.com/repos/{REPO}/issues/{NUMBER}/comments",
                json={
                    "body": f"ghstack bot failed: {msg}",
                },
            )
            exit(1)

    print(head_ref)
    must(
        head_ref and re.match(r"^gh/[A-Za-z0-9-]+/[0-9]+/head$", head_ref),
        "Not a ghstack PR",
    )
    orig_ref = head_ref.replace("/head", "/orig")
    print(":: Fetching newest main...")
    must(os.system("git fetch origin main") == 0, "Can't fetch main")
    print(":: Fetching orig branch...")
    must(os.system(f"git fetch origin {orig_ref}") == 0, "Can't fetch orig branch")

    proc = subprocess.Popen(
        "git log FETCH_HEAD...$(git merge-base FETCH_HEAD origin/main)",
        stdout=subprocess.PIPE,
        shell=True,
    )
    out, _ = proc.communicate()
    must(proc.wait() == 0, "`git log` command failed!")

    pr_numbers = re.findall(
        r"Pull[- ]Request[- ]resolved: https://github.com/.*?/pull/([0-9]+)",
        out.decode("utf-8"),
    )
    pr_numbers = list(map(int, pr_numbers))
    print(pr_numbers)
    must(pr_numbers and pr_numbers[0] == NUMBER, "Extracted PR numbers don't seem right!")


    # First, check that all PRs have approvals
    print(":: Checking approvals for all PRs...")
    for n in pr_numbers:
        print(f"Checking approvals for PR #{n}... ", end="")
        resp = gh.get(f"https://api.github.com/repos/{REPO}/pulls/{n}/reviews")
        must(resp.ok, f"Error getting reviews for PR #{n}!")
        reviews = resp.json()

        # Check if at least one approval exists
        has_approval = any(review["state"] == "APPROVED" for review in reviews)
        must(has_approval, f"PR #{n} has no approvals!")
        print("APPROVED!")

    # Function to check PR status with waiting logic
    def check_pr_status(pr_number):
        waiting_comment_posted = False
        start_time = time.time()

        def post_success_comment():
            gh.post(
                f"https://api.github.com/repos/{REPO}/issues/{pr_number}/comments",
                json={
                    "body": f"PR #{pr_number} status checks have completed successfully!",
                }
            )


        while True:
            # Get PR object with mergeable state
            resp = gh.get(
                f"https://api.github.com/repos/{REPO}/pulls/{pr_number}",
                headers={"Accept": "application/vnd.github.v3+json"}
            )
            must(resp.ok, f"Error getting PR #{pr_number}!")
            pr_obj = resp.json()

            # Check if GitHub is still calculating the mergeable state
            mergeable_state = pr_obj.get("mergeable_state", "unknown")
            if mergeable_state == "unknown":
                # Wait and try again - GitHub is still calculating
                time.sleep(2)
                resp = gh.get(
                    f"https://api.github.com/repos/{REPO}/pulls/{pr_number}",
                    headers={"Accept": "application/vnd.github.v3+json"}
                )
                must(resp.ok, f"Error getting PR #{pr_number} on retry!")
                pr_obj = resp.json()
                mergeable_state = pr_obj.get("mergeable_state", "unknown")

            # Handle different mergeable states
            if mergeable_state == "unstable":
                elapsed_time = time.time() - start_time
                if elapsed_time > MAX_WAIT_TIME:
                    must(False, f"PR #{pr_number} remained in unstable state for too long (over {MAX_WAIT_TIME // 60} minutes)!")

                # Check for failing status checks and check runs
                status_resp = gh.get(f"https://api.github.com/repos/{REPO}/commits/{pr_obj['head']['sha']}/status")
                must(status_resp.ok, f"Error getting status checks for PR #{pr_number}!")
                status_data = status_resp.json()

                statuses = status_data.get("statuses", [])
                # Check if any status checks are failing
                status_result, relevant_statuses = check_blocking_statuses(statuses)
                if status_result == "failed":
                    must(False, f"PR #{pr_number} has failing required status checks: {', '.join(relevant_statuses)}")
                elif status_result == "success":
                    post_success_comment()
                    return pr_obj

                # Post waiting comment if not already posted
                if not waiting_comment_posted:
                    print(f"\nPR #{pr_number} has pending required status checks. Waiting for status to change...")
                    gh.post(
                        f"https://api.github.com/repos/{REPO}/issues/{pr_number}/comments",
                        json={
                            "body": f"ghstack bot is waiting for PR #{pr_number} status checks to complete...",
                        }
                    )
                    waiting_comment_posted = True

                # Wait before checking again
                time.sleep(30)
                print(".", end="", flush=True)
                continue

            # Exit the loop if we've reached a terminal state
            if mergeable_state == "blocked":
                must(False, f"PR #{pr_number} is blocked from merging (possibly failing status checks)! Try doing `/land --force` if you want to ignore CI!")
            elif mergeable_state == "dirty":
                must(False, f"PR #{pr_number} has merge conflicts that need to be resolved!")
            elif mergeable_state == "clean":
                # If waiting comment was posted, post a follow-up
                if waiting_comment_posted:
                    post_success_comment()
                    return pr_obj
                return pr_obj
            else:
                must(False, f"PR #{pr_number} is not ready to merge (state: {mergeable_state})!")

    # Now check the status of the first PR with waiting behavior
    if pr_numbers:
        first_pr = pr_numbers[0]
        print(f":: Checking status for primary PR #{first_pr}... ", end="")
        check_pr_status(first_pr)
        print("SUCCESS!")


    print(":: All PRs are ready to be landed!")


if __name__ == "__main__":
    main()
