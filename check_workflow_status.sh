#!/bin/bash

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Script to check if a GitHub Actions workflow run has reached a Tmate debugging
# session step or has completed (with or without errors).
#
# Usage: ./check_workflow_status.sh [OPTIONS]
#   -u, --url URL                GitHub Actions workflow run URL (can deduce owner/repo/run-id)
#   -r, --run-id RUN_ID          GitHub Actions run ID
#   -o, --owner OWNER            Repository owner (default: canonical)
#   -n, --repo REPO              Repository name (default: jenkins-agent-operator)
#   -t, --token TOKEN            GitHub token (default: from GITHUB_TOKEN env var)
#   -w, --watch                  Watch mode - continuously monitor until completion or tmate
#   -i, --interval SECONDS       Polling interval in watch mode (default: 30)
#   -m, --mattermost             Send Mattermost notification when tmate session is active
#   -h, --help                   Show this help message
#
# Example:
#   ./check_workflow_status.sh --url https://github.com/canonical/jenkins-agent-operator/actions/runs/12345678
#   ./check_workflow_status.sh -r 12345678 -w -i 60
#   ./check_workflow_status.sh -u <url> -w -m

set -euo pipefail

# Default values
OWNER="canonical"
REPO="jenkins-agent-operator"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
RUN_ID=""
WORKFLOW_URL=""
WATCH_MODE=false
POLL_INTERVAL=30
MATTERMOST_NOTIFY=false
MATTERMOST_WEBHOOK="https://chat.canonical.com/hooks/c7d1fxpneprdfdpfwunxtkro4h"

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -u|--url)
            WORKFLOW_URL="$2"
            shift 2
            ;;
        -r|--run-id)
            RUN_ID="$2"
            shift 2
            ;;
        -o|--owner)
            OWNER="$2"
            shift 2
            ;;
        -n|--repo)
            REPO="$2"
            shift 2
            ;;
        -t|--token)
            GITHUB_TOKEN="$2"
            shift 2
            ;;
        -w|--watch)
            WATCH_MODE=true
            shift
            ;;
        -i|--interval)
            POLL_INTERVAL="$2"
            shift 2
            ;;
        -m|--mattermost)
            MATTERMOST_NOTIFY=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Check if a GitHub Actions workflow run has reached a Tmate debugging session or completed."
            echo ""
            echo "Options:"
            echo "  -u, --url URL                GitHub Actions workflow run URL (can deduce owner/repo/run-id)"
            echo "  -r, --run-id RUN_ID          GitHub Actions run ID"
            echo "  -o, --owner OWNER            Repository owner (default: canonical)"
            echo "  -n, --repo REPO              Repository name (default: jenkins-agent-operator)"
            echo "  -t, --token TOKEN            GitHub token (default: from GITHUB_TOKEN env var)"
            echo "  -w, --watch                  Watch mode - continuously monitor until completion or tmate"
            echo "  -i, --interval SECONDS       Polling interval in watch mode (default: 30)"
            echo "  -m, --mattermost             Send Mattermost notification when tmate session is active"
            echo "  -h, --help                   Show this help message"
            echo ""
            echo "Example:"
            echo "  $0 --url https://github.com/canonical/jenkins-agent-operator/actions/runs/12345678"
            echo "  $0 -r 12345678 -w -i 60"
            echo "  $0 -u <url> -w -m"
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option $1${NC}" >&2
            exit 1
            ;;
    esac
done

# Parse workflow URL if provided
if [[ -n "$WORKFLOW_URL" ]]; then
    # Expected format: https://github.com/OWNER/REPO/actions/runs/RUN_ID
    if [[ "$WORKFLOW_URL" =~ ^https://github\.com/([^/]+)/([^/]+)/actions/runs/([0-9]+) ]]; then
        OWNER="${BASH_REMATCH[1]}"
        REPO="${BASH_REMATCH[2]}"
        RUN_ID="${BASH_REMATCH[3]}"
        echo -e "${GREEN}Parsed from URL:${NC}"
        echo -e "  Owner: $OWNER"
        echo -e "  Repo: $REPO"
        echo -e "  Run ID: $RUN_ID"
        echo ""
    else
        echo -e "${RED}Error: Invalid workflow URL format${NC}" >&2
        echo -e "${RED}Expected: https://github.com/OWNER/REPO/actions/runs/RUN_ID${NC}" >&2
        exit 1
    fi
fi

# Validate required arguments
if [[ -z "$RUN_ID" ]]; then
    echo -e "${RED}Error: Run ID is required. Use -u/--url or -r/--run-id option.${NC}" >&2
    exit 1
fi

if [[ -z "$GITHUB_TOKEN" ]]; then
    echo -e "${RED}Error: GitHub token is required. Set GITHUB_TOKEN environment variable or use -t option.${NC}" >&2
    exit 1
fi

# GitHub API base URL
API_URL="https://api.github.com/repos/${OWNER}/${REPO}/actions/runs/${RUN_ID}"

# Function to make GitHub API calls
github_api_call() {
    local endpoint="$1"
    local response
    local http_code
    
    response=$(curl -s -w "\n%{http_code}" \
        -H "Authorization: token ${GITHUB_TOKEN}" \
        -H "Accept: application/vnd.github.v3+json" \
        "${endpoint}")
    
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')
    
    if [[ "$http_code" -ne 200 ]]; then
        echo -e "${RED}Error: GitHub API returned HTTP $http_code${NC}" >&2
        echo "$body" >&2
        return 1
    fi
    
    echo "$body"
}

# Function to send Mattermost notification
send_mattermost_notification() {
    local message="$1"
    local workflow_url="$2"
    
    local payload=$(cat <<EOF
{
    "text": "@charlie4284 ${message}",
    "attachments": [
        {
            "fallback": "Workflow URL: ${workflow_url}",
            "color": "#36a64f",
            "fields": [
                {
                    "title": "Workflow Run",
                    "value": "[View on GitHub](${workflow_url})",
                    "short": false
                }
            ]
        }
    ]
}
EOF
)
    
    local response=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$MATTERMOST_WEBHOOK")
    
    if [[ "$response" -eq 200 ]]; then
        echo -e "${GREEN}✓ Mattermost notification sent successfully${NC}"
    else
        echo -e "${YELLOW}⚠ Failed to send Mattermost notification (HTTP $response)${NC}"
    fi
}

# Function to check workflow status
check_workflow_status() {
    local run_data
    local jobs_data
    local workflow_name
    local workflow_status
    local workflow_conclusion
    local created_at
    local updated_at
    local html_url
    local send_notification="${1:-true}"  # Default to true, can be overridden
    
    # Get workflow run data
    run_data=$(github_api_call "$API_URL")
    
    if [[ $? -ne 0 ]]; then
        return 1
    fi
    
    workflow_name=$(echo "$run_data" | jq -r '.name')
    workflow_status=$(echo "$run_data" | jq -r '.status')
    workflow_conclusion=$(echo "$run_data" | jq -r '.conclusion')
    created_at=$(echo "$run_data" | jq -r '.created_at')
    updated_at=$(echo "$run_data" | jq -r '.updated_at')
    html_url=$(echo "$run_data" | jq -r '.html_url')
    
    echo -e "${CYAN}==================================================${NC}"
    echo -e "${BLUE}Workflow:${NC} $workflow_name"
    echo -e "${BLUE}Run ID:${NC} $RUN_ID"
    echo -e "${BLUE}Status:${NC} $workflow_status"
    echo -e "${BLUE}URL:${NC} $html_url"
    echo -e "${BLUE}Created:${NC} $created_at"
    echo -e "${BLUE}Updated:${NC} $updated_at"
    echo -e "${CYAN}==================================================${NC}"
    echo ""
    
    # Get jobs data
    jobs_data=$(github_api_call "${API_URL}/jobs")
    
    if [[ $? -ne 0 ]]; then
        return 1
    fi
    
    # Check each job
    local job_count=$(echo "$jobs_data" | jq '.jobs | length')
    local tmate_found=false
    local completed_jobs=0
    local failed_jobs=0
    local in_progress_jobs=0
    local integration_test_completed=false
    local integration_test_failed=false
    local integration_test_names=""
    
    echo -e "${BLUE}Jobs Status:${NC}"
    echo ""
    
    for ((i=0; i<job_count; i++)); do
        local job_name=$(echo "$jobs_data" | jq -r ".jobs[$i].name")
        local job_status=$(echo "$jobs_data" | jq -r ".jobs[$i].status")
        local job_conclusion=$(echo "$jobs_data" | jq -r ".jobs[$i].conclusion")
        local job_id=$(echo "$jobs_data" | jq -r ".jobs[$i].id")
        
        # Check if this is an integration test job
        local is_integration_test=false
        if echo "$job_name" | grep -iq "integration"; then
            is_integration_test=true
        fi
        
        # Determine status color and symbol
        local status_display=""
        case "$job_status" in
            "completed")
                completed_jobs=$((completed_jobs + 1))
                case "$job_conclusion" in
                    "success")
                        status_display="${GREEN}✓ COMPLETED (SUCCESS)${NC}"
                        if [[ "$is_integration_test" == true ]]; then
                            integration_test_completed=true
                            integration_test_names="${integration_test_names}${job_name} (✓ success)\n"
                        fi
                        ;;
                    "failure")
                        status_display="${RED}✗ COMPLETED (FAILURE)${NC}"
                        failed_jobs=$((failed_jobs + 1))
                        if [[ "$is_integration_test" == true ]]; then
                            integration_test_completed=true
                            integration_test_failed=true
                            integration_test_names="${integration_test_names}${job_name} (✗ failure)\n"
                        fi
                        ;;
                    "cancelled")
                        status_display="${YELLOW}⊘ COMPLETED (CANCELLED)${NC}"
                        if [[ "$is_integration_test" == true ]]; then
                            integration_test_completed=true
                            integration_test_names="${integration_test_names}${job_name} (⊘ cancelled)\n"
                        fi
                        ;;
                    "skipped")
                        status_display="${YELLOW}⊖ COMPLETED (SKIPPED)${NC}"
                        ;;
                    *)
                        status_display="${YELLOW}⊙ COMPLETED ($job_conclusion)${NC}"
                        if [[ "$is_integration_test" == true ]]; then
                            integration_test_completed=true
                            integration_test_names="${integration_test_names}${job_name} (⊙ $job_conclusion)\n"
                        fi
                        ;;
                esac
                ;;
            "in_progress")
                status_display="${CYAN}⟳ IN PROGRESS${NC}"
                in_progress_jobs=$((in_progress_jobs + 1))
                ;;
            "queued")
                status_display="${YELLOW}⋯ QUEUED${NC}"
                ;;
            *)
                status_display="${YELLOW}? $job_status${NC}"
                ;;
        esac
        
        echo -e "  ${BLUE}Job:${NC} $job_name"
        echo -e "    ${BLUE}Status:${NC} $status_display"
        
        # Check for tmate step in this job
        local steps_data=$(echo "$jobs_data" | jq -r ".jobs[$i].steps // []")
        local step_count=$(echo "$steps_data" | jq 'length')
        
        for ((j=0; j<step_count; j++)); do
            local step_name=$(echo "$steps_data" | jq -r ".[$j].name")
            local step_status=$(echo "$steps_data" | jq -r ".[$j].status")
            local step_conclusion=$(echo "$steps_data" | jq -r ".[$j].conclusion")
            
            # Check if this is a tmate step (various common naming patterns)
            if echo "$step_name" | grep -iq "tmate\|debug\|debugging"; then
                if [[ "$step_status" == "in_progress" ]]; then
                    echo -e "    ${GREEN}▶ TMATE DEBUGGING SESSION ACTIVE${NC}"
                    echo -e "      ${BLUE}Step:${NC} $step_name"
                    tmate_found=true
                elif [[ "$step_status" == "completed" ]]; then
                    echo -e "    ${YELLOW}⊙ Tmate step completed${NC}"
                    echo -e "      ${BLUE}Step:${NC} $step_name"
                    echo -e "      ${BLUE}Conclusion:${NC} $step_conclusion"
                fi
            fi
        done
        
        echo ""
    done
    
    echo -e "${CYAN}==================================================${NC}"
    echo -e "${BLUE}Summary:${NC}"
    echo -e "  Total Jobs: $job_count"
    echo -e "  Completed: $completed_jobs"
    echo -e "  Failed: $failed_jobs"
    echo -e "  In Progress: $in_progress_jobs"
    echo -e "  Workflow Status: $workflow_status"
    if [[ "$workflow_conclusion" != "null" ]]; then
        echo -e "  Workflow Conclusion: $workflow_conclusion"
    fi
    echo -e "${CYAN}==================================================${NC}"
    echo ""
    
    # Send notifications for integration test completion or tmate session
    if [[ "$MATTERMOST_NOTIFY" == true ]] && [[ "$send_notification" == "true" ]]; then
        if [[ "$tmate_found" == true ]]; then
            echo ""
            send_mattermost_notification "🔍 Tmate debugging session is now active!" "$html_url"
        elif [[ "$integration_test_completed" == true ]]; then
            echo ""
            if [[ "$integration_test_failed" == true ]]; then
                send_mattermost_notification "❌ Integration test(s) completed with failures:\n${integration_test_names}" "$html_url"
            else
                send_mattermost_notification "✅ Integration test(s) completed:\n${integration_test_names}" "$html_url"
            fi
        fi
    fi
    
    # Determine exit code and final message
    if [[ "$tmate_found" == true ]]; then
        echo -e "${GREEN}✓ Tmate debugging session is ACTIVE${NC}"
        echo -e "${CYAN}You can connect to the debugging session via the workflow logs.${NC}"
        return 10  # Special exit code for tmate found
    elif [[ "$workflow_status" == "completed" ]]; then
        if [[ "$workflow_conclusion" == "success" ]]; then
            echo -e "${GREEN}✓ Workflow completed successfully${NC}"
            return 0
        elif [[ "$workflow_conclusion" == "failure" ]]; then
            echo -e "${RED}✗ Workflow completed with failures${NC}"
            return 1
        else
            echo -e "${YELLOW}⊙ Workflow completed with conclusion: $workflow_conclusion${NC}"
            return 2
        fi
    else
        echo -e "${CYAN}⟳ Workflow is still running (status: $workflow_status)${NC}"
        return 3  # Still running
    fi
}


# Main execution
if [[ "$WATCH_MODE" == true ]]; then
    echo -e "${CYAN}Starting watch mode (polling every ${POLL_INTERVAL}s)...${NC}"
    echo -e "${CYAN}Press Ctrl+C to stop${NC}"
    echo ""

    notified_jobs=""

    while true; do
        # Get jobs data
        jobs_data=$(github_api_call "${API_URL}/jobs")
        job_count=$(echo "$jobs_data" | jq '.jobs | length')
        integration_jobs_done=0
        integration_jobs_total=0

        for ((i=0; i<job_count; i++)); do
            job_name=$(echo "$jobs_data" | jq -r ".jobs[$i].name")
            job_status=$(echo "$jobs_data" | jq -r ".jobs[$i].status")
            job_conclusion=$(echo "$jobs_data" | jq -r ".jobs[$i].conclusion")
            job_id=$(echo "$jobs_data" | jq -r ".jobs[$i].id")

            # Only consider integration test jobs (exclude Plan and Build charm)
            if echo "$job_name" | grep -iq "integration" && ! echo "$job_name" | grep -iq "plan\|build charm"; then
                integration_jobs_total=$((integration_jobs_total + 1))

                # Check if job already notified (using delimiter-separated string)
                job_notified=false
                if echo "$notified_jobs" | grep -q "|${job_name}|"; then
                    job_notified=true
                fi

                # Check for tmate step
                steps_data=$(echo "$jobs_data" | jq -r ".jobs[$i].steps // []")
                step_count=$(echo "$steps_data" | jq 'length')
                tmate_active=false
                for ((j=0; j<step_count; j++)); do
                    step_name=$(echo "$steps_data" | jq -r ".[$j].name")
                    step_status=$(echo "$steps_data" | jq -r ".[$j].status")
                    if echo "$step_name" | grep -iq "tmate\|debug\|debugging" && [[ "$step_status" == "in_progress" ]]; then
                        tmate_active=true
                    fi
                done

                # Notify if tmate active and not already notified
                if [[ "$tmate_active" == true && "$job_notified" == false ]]; then
                    if [[ "$MATTERMOST_NOTIFY" == true ]]; then
                        send_mattermost_notification "🔍 Tmate debugging session active for integration job: $job_name" "https://github.com/${OWNER}/${REPO}/actions/runs/${RUN_ID}"
                    fi
                    notified_jobs="${notified_jobs}|${job_name}|"
                fi

                # Notify if job completed and not already notified
                if [[ "$job_status" == "completed" && "$job_notified" == false ]]; then
                    if [[ "$MATTERMOST_NOTIFY" == true ]]; then
                        if [[ "$job_conclusion" == "success" ]]; then
                            send_mattermost_notification "✅ Integration test job completed: $job_name (success)" "https://github.com/${OWNER}/${REPO}/actions/runs/${RUN_ID}"
                        else
                            send_mattermost_notification "❌ Integration test job completed: $job_name ($job_conclusion)" "https://github.com/${OWNER}/${REPO}/actions/runs/${RUN_ID}"
                        fi
                    fi
                    notified_jobs="${notified_jobs}|${job_name}|"
                fi

                # Count completed jobs
                if [[ "$job_status" == "completed" ]]; then
                    integration_jobs_done=$((integration_jobs_done + 1))
                fi
            fi
        done

        # Exit only when all integration jobs are done
        if [[ $integration_jobs_total -gt 0 && $integration_jobs_done -eq $integration_jobs_total ]]; then
            echo -e "${GREEN}All integration test jobs completed. Exiting watch mode.${NC}"
            exit 0
        fi

        echo -e "${YELLOW}Next check in ${POLL_INTERVAL} seconds...${NC}"
        echo ""
        sleep "$POLL_INTERVAL"
    done
else
    # Single check mode
    check_workflow_status "true"
    exit $?
fi
