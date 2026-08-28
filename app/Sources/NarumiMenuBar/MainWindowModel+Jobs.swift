import Foundation
import NarumiMenuBarCore

extension MainWindowModel {
    var activeJobCount: Int { jobState.activeCount + max(pendingJobRequests, unresolvedJobRequestCount) }

    func beginJobRequest() {
        pendingJobRequests += 1
        notifyJobActivity()
    }

    func endJobRequest() {
        pendingJobRequests = max(0, pendingJobRequests - 1)
        notifyJobActivity()
    }

    func track(jobID: String) {
        guard !jobState.trackedIDs.contains(jobID) else { return }
        jobState.track(jobID: jobID)
        notifyJobActivity()
        Task { await refreshJobs() }
    }

    /// Only a definitive terminal status/not_found may remove a known active job.
    /// Transport errors retain it and keep automatic updates postponed.
    func refreshJobs() async {
        guard desktopSession.serverReachable else { return }
        for meeting in meetings {
            if let job = meeting.activeJob, !jobState.trackedIDs.contains(job.jobID) {
                jobState.track(jobID: job.jobID)
            }
        }
        notifyJobActivity()
        guard let token = jobState.beginRefresh() else { return }
        let generation = desktopSession.connectionGeneration
        let previousJobs = Dictionary(uniqueKeysWithValues: jobs.map { ($0.jobID, $0) })
        var refreshed: [Job] = []
        var missingIDs: Set<String> = []
        for id in token.ids {
            do {
                let job = try await client.jobStatus(jobID: id)
                refreshed.append(job)
            } catch let failure as ToolFailure where failure.code == "not_found" {
                missingIDs.insert(id)
            } catch {
                // A failed query is not evidence that a transcription/export stopped.
            }
            guard !Task.isCancelled, generation == desktopSession.connectionGeneration else {
                jobState.finishRefresh(token, jobs: [])
                return
            }
        }
        guard jobState.finishRefresh(token, jobs: refreshed, missingIDs: missingIDs) else { return }
        jobs = jobState.jobs
        notifyJobActivity()
        let selectedFinished = refreshed.contains { job in
            previousJobs[job.jobID]?.isActive == true && !job.isActive
                && job.meetingID == selectedMeetingID
        }
        if isPolling && selectedFinished {
            await loadDetail()
            minutes = nil
            transcript = nil
            await tabChanged()
        }
    }

    func cancel(jobID: String) async {
        beginJobRequest()
        defer { endJobRequest() }
        do {
            let job = try await client.cancelJob(jobID: jobID)
            showToast("ジョブをキャンセルしました (\(job.jobID): \(NarumiFormat.jobStatusLabel(job.status)))")
            await refreshJobs()
        } catch {
            report(error, title: "ジョブをキャンセルできません")
        }
    }

    func clearFinishedJobs() {
        jobState.clearFinished()
        jobs = jobState.jobs
        notifyJobActivity()
    }

    private func notifyJobActivity() {
        // Publishing also updates the count while a new job ID has no response yet.
        objectWillChange.send()
        hostActions.jobActivityChanged(activeJobCount > 0)
    }
}
