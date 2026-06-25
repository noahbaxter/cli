#!/usr/bin/env swift
// add_event.swift — creates a Calendar event with proper EKStructuredLocation (Maps pin)
// Args: CALENDAR TITLE DATE(yyyy-MM-dd) TIME_SECS DURATION_SECS LOCATION LAT LON [NOTES]

import Foundation
import EventKit
import CoreLocation

let args = CommandLine.arguments
guard args.count >= 9 else {
    fputs("usage: add_event.swift CAL TITLE DATE TIME_SECS DUR_SECS LOCATION LAT LON [NOTES]\n", stderr)
    exit(1)
}

let calName   = args[1]
let title     = args[2]
let dateStr   = args[3]
let timeSecs  = TimeInterval(args[4]) ?? 43200
let durSecs   = TimeInterval(args[5]) ?? 3600
let location  = args[6]
let lat       = Double(args[7])
let lon       = Double(args[8])
let notes     = args.count > 9 ? args[9] : ""

let store = EKEventStore()
let sema  = DispatchSemaphore(value: 0)
var exitCode: Int32 = 0

func requestCalendarAccess(completion: @escaping (Bool) -> Void) {
    if #available(macOS 14.0, *) {
        store.requestFullAccessToEvents { granted, _ in completion(granted) }
    } else {
        store.requestAccess(to: .event) { granted, _ in completion(granted) }
    }
}

requestCalendarAccess { granted in
    guard granted else {
        fputs("Calendar access denied\n", stderr)
        exitCode = 1
        sema.signal()
        return
    }

    guard let cal = store.calendars(for: .event).first(where: { $0.title == calName }) else {
        fputs("Calendar '\(calName)' not found\n", stderr)
        exitCode = 1
        sema.signal()
        return
    }

    let fmt = DateFormatter()
    fmt.dateFormat = "yyyy-MM-dd"
    fmt.timeZone = .current
    guard let base = fmt.date(from: dateStr) else {
        fputs("Invalid date: \(dateStr)\n", stderr)
        exitCode = 1
        sema.signal()
        return
    }

    let start = base.addingTimeInterval(timeSecs)
    let end   = start.addingTimeInterval(durSecs)

    let event = EKEvent(eventStore: store)
    event.title     = title
    event.startDate = start
    event.endDate   = end
    event.calendar  = cal
    if !notes.isEmpty { event.notes = notes }

    if !location.isEmpty {
        let sl = EKStructuredLocation(title: location)
        if let lat = lat, let lon = lon {
            sl.geoLocation = CLLocation(latitude: lat, longitude: lon)
            sl.radius = 100
        }
        event.structuredLocation = sl
        event.location = location
    }

    do {
        try store.save(event, span: .thisEvent)
        print("ok")
    } catch {
        fputs("Error saving event: \(error)\n", stderr)
        exitCode = 1
    }
    sema.signal()
}
sema.wait()
exit(exitCode)
