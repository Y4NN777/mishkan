import XCTest
@testable import Fixture

final class FixtureTests: XCTestCase {
    func testFixture() {
        XCTAssertEqual(fixture, "swift-ok")
    }
}
