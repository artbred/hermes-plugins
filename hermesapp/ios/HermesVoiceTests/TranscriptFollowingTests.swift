import SwiftUI
import Testing
@testable import HermesVoice

@Suite("Following the newest reply")
struct TranscriptFollowingTests {
    @Test("Layout and non-user scroll phases never take over history",
          arguments: [ScrollPhase.tracking, .interacting, .decelerating, .animating, .idle])
    func nonUserPositioning(phase: ScrollPhase) {
        var following = TranscriptFollowing()
        let anchors = following.phaseChanged(to: phase, at: .showing(), isPositionedByUser: false)
        #expect(!anchors)
    }

    @Test("A history reader stays put through reply growth and keyboard changes until reaching the end")
    func scrollingAwayAndBack() {
        var following = TranscriptFollowing()
        let whileAway = following.phaseChanged(
            to: .idle, at: .showing(distanceFromEnd: 40), isPositionedByUser: true
        )
        #expect(!whileAway)
        let afterGrowth = following.phaseChanged(
            to: .idle, at: .showing(content: 3_500, viewport: 380, distanceFromEnd: 1_700),
            isPositionedByUser: true
        )
        #expect(!afterGrowth)
        let whileDecelerating = following.phaseChanged(
            to: .decelerating, at: .showing(content: 3_500, viewport: 380), isPositionedByUser: true
        )
        #expect(!whileDecelerating)
        let atEnd = following.phaseChanged(
            to: .idle, at: .showing(content: 3_500, viewport: 380), isPositionedByUser: true
        )
        #expect(atEnd)
    }

    @Test("A completed short-chat bounce rearms following without treating blank space as history",
          arguments: [CGFloat(300), 560, 600])
    func shortTranscript(contentHeight: CGFloat) {
        var following = TranscriptFollowing()
        let geometry = TranscriptGeometry(
            contentHeight: contentHeight, visibleRect: CGRect(x: 0, y: -400, width: 390, height: 600)
        )
        let firstIdle = following.phaseChanged(to: .idle, at: geometry, isPositionedByUser: true)
        let repeatedIdle = following.phaseChanged(to: .idle, at: geometry, isPositionedByUser: true)
        #expect(firstIdle)
        #expect(!repeatedIdle)
    }

    @Test("Following resumes at the actual visible end, not within an arbitrary near-bottom distance",
          arguments: [CGFloat(-20), 0, 1, 40, 64])
    func visibleEndBoundary(distanceFromEnd: CGFloat) {
        var following = TranscriptFollowing()
        let anchors = following.phaseChanged(
            to: .idle, at: .showing(distanceFromEnd: distanceFromEnd), isPositionedByUser: true
        )
        #expect(anchors == (distanceFromEnd <= 0))
    }

    @Test("A pending bottom assignment rearms only once and does not swallow later user navigation")
    func positionAcknowledgement() {
        var following = TranscriptFollowing()
        let firstIdle = following.phaseChanged(to: .idle, at: .showing(), isPositionedByUser: true)
        let repeatedIdle = following.phaseChanged(to: .idle, at: .showing(), isPositionedByUser: true)
        let whilePending = following.phaseChanged(to: .interacting, at: .showing(), isPositionedByUser: true)
        #expect(firstIdle)
        #expect(!repeatedIdle)
        #expect(!whilePending)
        following.positionChanged(isPositionedByUser: false)
        let laterHistory = following.phaseChanged(
            to: .idle, at: .showing(distanceFromEnd: 600), isPositionedByUser: true
        )
        #expect(!laterHistory)
        let laterEnd = following.phaseChanged(to: .idle, at: .showing(), isPositionedByUser: true)
        #expect(laterEnd)
    }

    @Test("The keyboard-visible rect determines whether manual scrolling has reached the end")
    func endIsAboveComposerAndKeyboard() {
        var following = TranscriptFollowing()
        let end = TranscriptGeometry(
            contentHeight: 2_000, visibleRect: CGRect(x: 0, y: 1_620, width: 390, height: 380)
        )
        let hidden = TranscriptGeometry(
            contentHeight: 2_000, visibleRect: CGRect(x: 0, y: 1_620, width: 390, height: 80)
        )
        let whileHidden = following.phaseChanged(to: .idle, at: hidden, isPositionedByUser: true)
        let atEnd = following.phaseChanged(to: .idle, at: end, isPositionedByUser: true)
        #expect(!whileHidden)
        #expect(atEnd)
    }
}

private extension TranscriptGeometry {
    /// Uses the already-inset visible viewport; its end is `distanceFromEnd` points short of the newest content.
    static func showing(content: CGFloat = 3_000, viewport: CGFloat = 600, distanceFromEnd: CGFloat = 0) -> Self {
        Self(
            contentHeight: content,
            visibleRect: CGRect(x: 0, y: content - viewport - distanceFromEnd, width: 390, height: viewport)
        )
    }
}
