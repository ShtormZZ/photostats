# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the real Timeline UI with an installed Chromium browser and fake photos.

No browser packages or downloads are required. The local server and browser
profile are temporary. --no-sandbox is available for restricted test hosts.
"""

import argparse
import base64
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from tools.test_timeline import make_fixture
from flask import Response, request
from PIL import Image
from werkzeug.serving import WSGIRequestHandler, make_server


SCRIPT = r"""
<script>
(async () => {
  const checks = JSON.parse(sessionStorage.getItem("timeline-test-checks") || "[]");
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const check = (name, ok) => { if (!ok) throw new Error(name); checks.push(name); };
  const wait = async (predicate, name) => {
    for (let i = 0; i < 400; i++){ if (predicate()) return; await delay(20); }
    throw new Error("Timed out: " + name);
  };
  const settled = () => wait(() => photoTimeline.started && !photoTimeline.loading &&
    !photoTimeline.error && !photoTimeline.updating.size && !photoTimeline.days.some(d => d.allLoading), "Timeline settled");
  const change = (id, value) => {
    const input = $(id);
    if (input.type === "checkbox") input.checked = value; else input.value = value;
    input.dispatchEvent(new Event("change", {bubbles:true}));
  };
  const byTime = items => items.slice().sort((a, b) => a.taken.localeCompare(b.taken) || a.id - b.id);
  const captureOrder = items => [items.filter(it => it.mark === "great"),
    items.filter(it => it.mark !== "great" && it.mark !== "bad"),
    items.filter(it => it.mark === "bad")].flatMap(byTime);
  const ids = items => items.map(it => it.id);
  const inCaptureOrder = items => JSON.stringify(ids(items)) === JSON.stringify(ids(captureOrder(items)));
  const revealDay = async day => {
    const index = photoTimeline.days.indexOf(day);
    const top = $("timelineStage").getBoundingClientRect().top + scrollY;
    scrollTo(0, top + photoTimeline.offsets[index]); schedulePhotoTimeline();
    await wait(() => photoTimeline.mounted.has(day.day), "day mounted");
    return photoTimeline.mounted.get(day.day);
  };
  const finish = result => {
    const output = document.createElement("pre"); output.id = "timelineTestResult";
    output.hidden = true;
    result.width = innerWidth;
    output.textContent = JSON.stringify(result); document.body.appendChild(output);
  };
  try {
    await wait(() => state.page === "timeline" && photoTimeline.days.length, "initial Timeline");
    await settled();
    check("JavaScript started without errors", !window.timelineTestErrors.length);
    if (sessionStorage.getItem("timeline-test-reload")){
      check("Reload resets tags, rating, unrated and limit settings", !photoTimeline.tags.length &&
        photoTimeline.ratingMode === "good" && photoTimeline.includeUnrated &&
        !photoTimeline.limitDaily && !$("timelineLimit").checked &&
        $("timelineRating").value === "good" && $("timelineUnrated").checked);
      check("Reload clears the extra filter and its option", !photoTimeline.extraType && !photoTimeline.extraValue &&
        $("timelineFilter").value === "" && $("timelineFilterSummary").getAttribute("aria-disabled") === "true");
      check("No horizontal overflow", document.documentElement.scrollWidth <= innerWidth + 1);
      return finish({ok:true, checks, errors:window.timelineTestErrors});
    }
    check("Limit defaults off and days automatically expand", !photoTimeline.limitDaily &&
      photoTimeline.days.every(day => day.expanded));
    check("Show all is hidden without a daily limit", [...document.querySelectorAll(".timeline-toggle-all")].every(b => b.hidden));
    check("No horizontal overflow", document.documentElement.scrollWidth <= innerWidth + 1);

    change("timelineUnrated", false); await settled();
    check("Rated-only also expands automatically without a limit", photoTimeline.days.every(day => day.expanded));
    change("timelineLimit", true); await settled();
    const large = photoTimeline.days.find(day => day.total > 15);
    check("Rated-only starts with fifteen highlights", large && !large.expanded && large.items.length === 15 &&
      large.items.every(it => it.mark || it.rating));
    const rawHighlights = await fetch(timelineURL({day:large.day})).then(r => r.json());
    check("Client sorts highlights from unordered server records", inCaptureOrder(large.items) &&
      !inCaptureOrder(rawHighlights.days[0].items) &&
      JSON.stringify(ids(large.items).sort((a,b) => a-b)) ===
      JSON.stringify(ids(rawHighlights.days[0].items).sort((a,b) => a-b)));
    let row = await revealDay(large);
    const previewPhoto = large.items[0];
    const previewButton = [...row.photoNodes.values()].find(button => button.dataset.pid === String(previewPhoto.id));
    check("Highlight tooltip has shooting metadata and only present annotation rows",
      previewButton.title.split("\n").length === 4 + Number(!!previewPhoto.lens) + Number(tagsOf(previewPhoto).length > 0) + Number(!!previewPhoto.comment) &&
      previewButton.title.includes("Camera model: ") && previewButton.title.includes("\nLens: ") === !!previewPhoto.lens &&
      previewButton.title.includes("Focal length: ") && previewButton.title.includes(" · Aperture: ") &&
      previewButton.title.includes(" · ISO: ") && previewButton.title.includes(" · Shutter speed: ") &&
      previewButton.title.includes("\nTags: ") === (tagsOf(previewPhoto).length > 0) &&
      previewButton.title.includes("\nComment: ") === !!previewPhoto.comment);
    check("Rated-only offers Show all", !row.querySelector(".timeline-toggle-all").hidden);
    row.querySelector(".timeline-toggle-all").click(); await settled();
    if (large.allItems.length <= 15){
      scrollBy(0, (photoTimeline.tile + 6) * 16); schedulePhotoTimeline();
      await wait(() => large.allItems.length > 15 && !large.allLoading, "expanded next page");
    }
    check("Show all retains rating filters", large.expanded && large.allItems.length > 15 &&
      large.allItems.every(it => ["great", "5", "4"].includes(effRating(it))));
    check("Full days are chronological before showing thumbnails", large.allNext === null &&
      large.allItems.length === large.total && inCaptureOrder(large.allItems));
    check("Equal capture times use stable photo ID order", large.allItems.some((it, i, items) =>
      i && it.taken === items[i - 1].taken && it.id > items[i - 1].id));
    row = await revealDay(large);
    const firstVisible = row.querySelector(".timeline-photo[data-pid]");
    firstVisible.click(); await wait(() => state.timelineDay === large, "chronological photo card");
    const current = state.deck.findIndex(it => it.id === state.shown.id);
    const following = state.deck[current + 1];
    if (following){
      $("detail").querySelector(".nav.next").click();
      await wait(() => state.shown.id === following.id, "chronological next photo");
    }
    check("Card navigation follows displayed capture order", inCaptureOrder(state.deck));
    closeDetail();

    change("timelineRating", "all"); change("timelineUnrated", true); await settled();
    const paused = photoTimeline.days.find(day => day.day === "2020-01-03");
    row = await revealDay(paused);
    const metadataFetch = window.fetch.bind(window);
    let holdBatch = true, releaseBatch;
    window.fetch = (url, options) => {
      const request = new URL(url, location.origin);
      if (holdBatch && request.pathname === "/api/timeline/day" && +request.searchParams.get("offset") > 0){
        holdBatch = false;
        return metadataFetch(url, options).then(r => r.text()).then(text => new Promise(resolve => {
          releaseBatch = () => resolve(new Response(text, {headers:{"Content-Type":"application/json"}}));
        }));
      }
      return metadataFetch(url, options);
    };
    row.querySelector(".timeline-toggle-all").click();
    await wait(() => releaseBatch, "second metadata batch held");
    renderPhotoTimeline(); row = photoTimeline.mounted.get(paused.day);
    check("Partial full-day metadata is not shown in a misleading order", paused.allItems.length < paused.total &&
      !row.querySelector(".timeline-photo[data-pid]") && row.querySelector(".progress").textContent.includes("500/"));
    const pausedLoad = paused.allTask;
    row.querySelector(".timeline-toggle-all").click();
    releaseBatch(); await pausedLoad; await settled(); window.fetch = metadataFetch;
    check("Collapsing pauses metadata loading and restores sorted highlights", !paused.expanded &&
      paused.allNext !== null && inCaptureOrder(paused.items));
    row = await revealDay(paused); row.querySelector(".timeline-toggle-all").click();
    await settled();
    check("Expanding resumes the cursor and sorts the whole day", paused.allNext === null &&
      paused.allItems.length === paused.total && new Set(ids(paused.allItems)).size === paused.total &&
      inCaptureOrder(paused.allItems));
    const greats = paused.allItems.filter(it => it.mark === "great");
    const bads = paused.allItems.filter(it => it.mark === "bad");
    const ordinary = paused.allItems.filter(it => it.mark !== "great" && it.mark !== "bad");
    check("Every 5+ photo leads the day even if taken later", greats.length > 0 &&
      JSON.stringify(ids(paused.allItems.slice(0, greats.length))) === JSON.stringify(ids(byTime(greats))) &&
      greats.some(great => ordinary.some(other => great.taken > other.taken)));
    check("Every bad photo follows unrated even if taken earlier", bads.length > 0 &&
      JSON.stringify(ids(paused.allItems.slice(-bads.length))) === JSON.stringify(ids(byTime(bads))) &&
      bads.some(bad => ordinary.some(other => !other.mark && !other.rating && bad.taken < other.taken)));
    check("Other ratings and unrated share chronological order", inCaptureOrder(ordinary) &&
      JSON.stringify(ids(ordinary)) === JSON.stringify(ids(byTime(ordinary))));

    const unrated = ordinary.find(it => !it.mark && !it.rating);
    row = await revealDay(paused);
    await openTimelinePhoto(paused, unrated.id);
    await wait(() => state.timelineDay === paused && state.shown.id === unrated.id, "unrated card for ordering edit");
    $("detail").querySelector(".rate .great").click();
    await wait(() => state.shown.mark === "great" && !photoTimeline.updating.size, "promote photo to 5+");
    // The refreshed highlights can arrive before the full-day metadata prefetch.
    await settled();
    check("Rating edit moves a photo into the leading time-sorted group", inCaptureOrder(paused.allItems) &&
      paused.allItems.slice(0, greats.length + 1).some(it => it.id === unrated.id) && inCaptureOrder(state.deck));
    $("detail").querySelector(".rate .bad").click();
    await wait(() => state.shown.mark === "bad" && !photoTimeline.updating.size, "move photo to bad");
    await settled();
    check("Rating edit moves a photo into the trailing time-sorted group", inCaptureOrder(paused.allItems) &&
      paused.allItems.slice(-(bads.length + 1)).some(it => it.id === unrated.id) && inCaptureOrder(state.deck));
    $("detail").querySelector(".rate .bad").click();
    await wait(() => !state.shown.mark && !photoTimeline.updating.size, "restore unrated photo");
    await settled();
    closeDetail();

    const annotated = paused.allItems.find(it => it.mark === "great" && it.camera && it.lens && it.focal && it.fnumber);
    const originalTags = tagsOf(annotated), originalComment = annotated.comment || "";
    row = await revealDay(paused);
    await openTimelinePhoto(paused, annotated.id);
    await wait(() => state.shown.id === annotated.id && state.timelineDay === paused, "annotation card");
    const card = state.shown;
    const thumb = () => [...photoTimeline.mounted.get(paused.day).photoNodes.values()]
      .find(button => button.dataset.pid === String(card.id));
    const writeTags = async list => {
      saveTags(card, list);
      await wait(() => JSON.stringify(tagsOf(card)) === JSON.stringify(list) && !photoTimeline.updating.size, "tags saved");
      await settled(); await revealDay(paused);
    };
    const writeComment = async text => {
      saveComment(card, text);
      await wait(() => (card.comment || "") === text && (text
        ? thumb().title.endsWith("Comment: " + text) : !thumb().title.includes("\nComment: ")), "comment saved");
    };
    const lines = thumb().title.split("\n");
    check("Tooltip shows real metadata values with settings on one line", lines[0] === card.filename && lines[1] === card.taken &&
      lines[2] === "Camera model: " + card.camera && lines[3] === "Lens: " + card.lens &&
      lines[4] === "Focal length: " + card.focal + " mm · Aperture: f/" + card.fnumber +
        " · ISO: 400 · Shutter speed: 1/125");
    let hoverRequests = 0;
    const hoverFetch = window.fetch.bind(window);
    window.fetch = (url, options) => {
      if (String(url).startsWith("/api/timeline")) hoverRequests++;
      return hoverFetch(url, options);
    };
    thumb().dispatchEvent(new MouseEvent("mouseover", {bubbles:true})); await delay(50);
    window.fetch = hoverFetch;
    check("Hover needs no additional Timeline request", hoverRequests === 0);
    await writeTags([]); await writeComment("");
    check("Both annotation badges are hidden without tags or comment", thumb().querySelector(".timeline-tags-badge").hidden &&
      thumb().querySelector(".timeline-comment-badge").hidden && thumb().querySelector(".timeline-annotations").hidden &&
      !thumb().title.includes("\nTags: ") && !thumb().title.includes("\nComment: ") && thumb().title.split("\n").length === 5);
    await writeTags(["kids"]);
    check("Tags alone show only the tag badge and update the tooltip", !thumb().querySelector(".timeline-tags-badge").hidden &&
      thumb().querySelector(".timeline-comment-badge").hidden &&
      thumb().title.endsWith("\nTags: kids") && !thumb().title.includes("\nComment: "));
    const indicator = thumb().querySelector(".timeline-tags-badge").getBoundingClientRect();
    const ratingBadge = thumb().querySelector(".timeline-badge").getBoundingClientRect();
    const tile = thumb().getBoundingClientRect();
    check("Tag badge is at bottom right opposite the rating", indicator.right <= tile.right &&
      indicator.left > ratingBadge.right && Math.abs(indicator.bottom - ratingBadge.bottom) <= 1);
    await writeTags([]);
    await writeComment('Note <b> & "quoted"');
    check("Comment alone shows only the comment badge and remains plain text", !thumb().querySelector(".timeline-comment-badge").hidden &&
      thumb().querySelector(".timeline-tags-badge").hidden &&
      thumb().title.endsWith('Comment: Note <b> & "quoted"') && !thumb().title.includes("\nTags: ") && !thumb().querySelector("b"));
    await writeTags(["kids"]);
    check("Tags and comment show two distinct badges", thumb().querySelectorAll(".timeline-annotation:not([hidden])").length === 2 &&
      thumb().querySelector(".timeline-tags-badge").innerHTML !== thumb().querySelector(".timeline-comment-badge").innerHTML &&
      thumb().title.includes('\nTags: kids\nComment: Note <b> & "quoted"'));
    const tagBox = thumb().querySelector(".timeline-tags-badge").getBoundingClientRect();
    const commentBox = thumb().querySelector(".timeline-comment-badge").getBoundingClientRect();
    check("Both badges fit side by side without covering the rating", tagBox.right < commentBox.left &&
      tagBox.left > thumb().querySelector(".timeline-badge").getBoundingClientRect().right &&
      commentBox.right <= thumb().getBoundingClientRect().right && Math.abs(tagBox.bottom - commentBox.bottom) <= 1);
    const smallest = createTimelinePhoto(paused, {...card, mark:"5"});
    smallest.style.width = "96px"; smallest.style.height = "96px"; document.body.appendChild(smallest);
    check("Both icons fit next to five stars on a small thumbnail",
      smallest.querySelector(".timeline-tags-badge").getBoundingClientRect().left >=
      smallest.querySelector(".timeline-badge").getBoundingClientRect().right);
    smallest.remove();
    await writeComment(originalComment); await writeTags(originalTags);
    closeDetail();
    change("timelineRating", "good"); change("timelineUnrated", false); await settled();

    $("timelineTags").open = true;
    await wait(() => $("timelineTagOptions").querySelector('input[value="kids"]'), "tag options");
    check("Open tag menu fits the viewport", document.documentElement.scrollWidth <= innerWidth + 1 &&
      document.querySelector(".timeline-tag-menu").getBoundingClientRect().left >= 0);
    $("timelineTagOptions").querySelector('input[value="kids"]').click(); await settled();
    $("timelineTagOptions").querySelector('input[value="море"]').click(); await settled();
    check("Tags select any and render removable chips", photoTimeline.tags.length === 2 &&
      $("timelineTagChips").children.length === 2 && new URL(timelineURL(), location.origin).searchParams.getAll("tag").length === 2);
    $("timelineTagSearch").value = "мор"; $("timelineTagSearch").dispatchEvent(new Event("input"));
    await wait(() => $("timelineTagOptions").children.length === 1 &&
      $("timelineTagOptions").querySelector('input[value="море"]'), "tag search");
    check("Tag search retains checked selection", document.querySelector("#timelineTagOptions input").checked);
    $("timelineTagsClear").click(); await settled();
    check("Clear tags restores unfiltered tags", !photoTimeline.tags.length && !$("timelineTagChips").children.length);
    $("timelineTags").open = false;

    // The presentation limit must be applied after every rating/unrated/tag
    // combination, with Show all exposing exactly the paged, filtered result.
    for (const mode of ["all", "good", "great"]){
      for (const unrated of [false, true]){
        for (const selected of [["kids"], ["kids", "море"]]){
          change("timelineRating", mode); change("timelineUnrated", unrated);
          $("timelineTagsClear").click();
          selected.forEach(tag => selectTimelineTag(tag, true));
          await settled();
          const chosen = photoTimeline.days.find(day => day.day === "2020-01-03");
          const highlights = ids(chosen.items);
          const description = mode + "/unrated=" + unrated + "/tags=" + selected.length;
          check("Fifteen after filters: " + description, photoTimeline.limitDaily &&
            !chosen.expanded && chosen.items.length === 15 && photoTimeline.days.every(day => day.items.length <= 15));
          let target = await revealDay(chosen);
          check("Show all is offered after filters: " + description,
            !target.querySelector(".timeline-toggle-all").hidden);
          target.querySelector(".timeline-toggle-all").click();
          while (chosen.allNext !== null){ await loadAllTimelineDay(chosen); }
          await settled();
          const expected = [];
          let offset = 0;
          while (true){
            const query = new URLSearchParams({day:chosen.day, rating_mode:mode,
              unrated:unrated ? "1" : "0", seed:photoTimeline.seed, limit:137, offset});
            selected.forEach(tag => query.append("tag", tag));
            const page = await fetch("/api/timeline/day?" + query).then(r => r.json());
            expected.push(...page.items);
            if (page.next === null) break;
            offset = page.next;
          }
          check("Show all contains every filtered photo exactly once: " + description,
            JSON.stringify(ids(chosen.allItems)) === JSON.stringify(ids(captureOrder(expected))) &&
            chosen.total === expected.length && chosen.expanded);
          target = await revealDay(chosen);
          target.querySelector(".timeline-toggle-all").click(); await settled();
          check("Collapse restores the same filtered fifteen: " + description,
            !chosen.expanded && inCaptureOrder(chosen.items) && JSON.stringify(ids(chosen.items)) === JSON.stringify(highlights));
          const small = photoTimeline.days.find(day => day.total <= 15);
          if (small){
            target = await revealDay(small);
            check("No Show all for fifteen or fewer matches: " + description,
              target.querySelector(".timeline-toggle-all").hidden);
          }
          change("timelineLimit", false); await settled();
          check("Limit off preserves filters and shows all automatically: " + description,
            photoTimeline.days.every(day => day.expanded) &&
            photoTimeline.days.find(day => day.day === chosen.day).total === expected.length &&
            photoTimeline.ratingMode === mode && photoTimeline.includeUnrated === unrated &&
            JSON.stringify(photoTimeline.tags) === JSON.stringify(selected));
          change("timelineLimit", true); await settled();
        }
      }
    }
    $("timelineTagsClear").click(); await settled();
    change("timelineUnrated", false); await settled();

    change("timelineRating", "all"); await settled();
    const allDay = photoTimeline.days.find(day => day.day === "2020-01-02");
    check("Good to bad includes a bad-only day", allDay?.items[0]?.mark === "bad");
    change("timelineRating", "great"); await settled();
    check("5+ mode excludes other ratings", photoTimeline.days.every(day => day.items.every(it => it.mark === "great")));
    change("timelineUnrated", true); await settled();
    check("Unrated is independent of 5+ mode", photoTimeline.days.some(day => day.day === "2020-01-04"));

    const field = async value => {
      change("timelineFilter", value);
      await wait(() => !timelineOptions.loading && timelineOptions.items.length, "extra options for " + value);
    };
    const searchOptions = async (query, expected) => {
      $("timelineOptionSearch").value = query;
      $("timelineOptionSearch").dispatchEvent(new Event("input"));
      await wait(() => !timelineOptions.loading && timelineOptions.items.length === 1 &&
        timelineOptions.items[0].k === expected, "option search " + query);
    };
    const chooseOption = async value => {
      const button = [...$("timelineOptionList").querySelectorAll("button")].find(b => b.dataset.value === value);
      check("Option exists: " + value, !!button);
      button.click(); await settled();
      check("One option is applied: " + value, photoTimeline.extraValue === value &&
        timelineParams().getAll("extra_value").length === 1);
    };
    await field("camera");
    check("Long option lists load thirty at a time", timelineOptions.items.length === 30 && timelineOptions.next === 30);
    check("Option popup fits narrow and wide viewports", document.documentElement.scrollWidth <= innerWidth + 1 &&
      document.querySelector(".timeline-extra-menu").getBoundingClientRect().left >= 0);
    $("timelineOptionMore").click();
    await wait(() => !timelineOptions.loading && timelineOptions.items.length === 60, "more camera options");
    check("More options have no duplicates", new Set(timelineOptions.items.map(item => item.k)).size === 60);
    await searchOptions("Camera 02", "Camera 02");
    $("timelineOptionSearch").dispatchEvent(new KeyboardEvent("keydown", {key:"ArrowDown", bubbles:true}));
    check("Option list supports keyboard focus", document.activeElement.getAttribute("role") === "option");
    await chooseOption("Camera 02");
    check("Selected option stays visible", $("timelineFilterSummary").textContent === "Camera 02");
    const cameraDay = photoTimeline.days[0];
    let cameraRow = await revealDay(cameraDay);
    cameraRow.querySelector(".timeline-photo").click();
    await wait(() => state.timelineDay === cameraDay, "filtered camera card");
    check("Card obeys the selected camera", state.shown.camera === "Camera 02" && matchesTimelinePhoto(state.shown));
    closeDetail();
    await field("lens");
    check("Changing field clears the old option", !photoTimeline.extraValue && timelineParams().get("extra_value") === null);
    await searchOptions("юпитер", "Юпитер ' %_! 50 mm");
    await chooseOption("Юпитер ' %_! 50 mm");
    await field("year"); await chooseOption("2020");
    const yearDay = photoTimeline.days.find(day => day.total > 15);
    check("Fifteen is applied after the extra filter", !yearDay.expanded && yearDay.items.length === 15);
    const yearRow = await revealDay(yearDay);
    yearRow.querySelector(".timeline-toggle-all").click();
    while (yearDay.allNext !== null) await loadAllTimelineDay(yearDay);
    check("Show all retains the extra option", yearDay.allItems.length === yearDay.total && timelineParams().get("extra_value") === "2020");
    await field("focal");
    await searchOptions("36-50", "36-51"); await chooseOption("36-51");
    await field("aperture");
    await searchOptions("2.8", "2.1-3.0"); await chooseOption("2.1-3.0");
    check("Aperture and focal use separate range selections", photoTimeline.extraType === "aperture" &&
      photoTimeline.extraValue === "2.1-3.0");
    $("timelineFilterOptions").open = true;
    await wait(() => !timelineOptions.loading && timelineOptions.items.length, "reopen aperture options");
    $("timelineOptionClear").click(); await settled();
    check("Clear option keeps the chosen filter type", !photoTimeline.extraValue && photoTimeline.extraType === "aperture");
    $("timelineOptionSearch").value = "not present";
    $("timelineOptionSearch").dispatchEvent(new Event("input"));
    await wait(() => $("timelineOptionStatus").textContent === "No matching options.", "empty option search");
    check("Empty search results do not add a filter", !photoTimeline.extraValue);

    const optionsFetch = window.fetch.bind(window);
    let holdOptions = true, releaseOptions;
    window.fetch = (url, options) => {
      if (holdOptions && String(url).startsWith("/api/timeline/options")){
        holdOptions = false;
        return optionsFetch(url, options).then(r => r.text()).then(text => new Promise(resolve => {
          releaseOptions = () => resolve(new Response(text, {headers:{"Content-Type":"application/json"}}));
        }));
      }
      return optionsFetch(url, options);
    };
    change("timelineFilter", "camera");
    await wait(() => releaseOptions, "old option request held");
    await field("lens"); releaseOptions(); await delay(100); window.fetch = optionsFetch;
    check("Old options cannot overwrite another field", photoTimeline.extraType === "lens" &&
      timelineOptions.items.every(item => item.k === NONE || item.l.startsWith("Lens") || item.l.startsWith("Юпитер")));
    change("timelineFilter", ""); await settled();
    check("No extra filter clears both values", !photoTimeline.extraType && !photoTimeline.extraValue &&
      !timelineParams().has("extra_type") && !timelineParams().has("extra_value"));

    // Deliver an obsolete response even after AbortController cancellation.
    const realFetch = window.fetch.bind(window);
    let hold = true, release;
    window.fetch = (url, options) => {
      if (hold && String(url).startsWith("/api/timeline/photos")){
        hold = false;
        return realFetch(url, options).then(r => r.text()).then(text => new Promise(resolve => {
          release = () => resolve(new Response(text, {headers:{"Content-Type":"application/json"}}));
        }));
      }
      return realFetch(url, options);
    };
    change("timelineRating", "all");
    await wait(() => release, "held response");
    change("timelineUnrated", false); change("timelineRating", "great"); await settled();
    release(); await delay(100); window.fetch = realFetch;
    check("Old filter response cannot replace the new selection", photoTimeline.ratingMode === "great" &&
      !photoTimeline.includeUnrated && photoTimeline.days.every(day => day.items.every(it => it.mark === "great")));

    selectTimelineTag("other", true); await settled();
    let day = photoTimeline.days[0]; row = await revealDay(day);
    row.querySelector(".timeline-photo").click(); await wait(() => state.timelineDay === day, "photo card");
    $("detail").querySelector(".rate .bad").click();
    await wait(() => !photoTimeline.days.length && !state.timelineDay, "rating removes day");
    check("Rating edit removes nonmatching photo and day", !$("veil").classList.contains("on"));
    change("timelineRating", "all"); await settled();
    day = photoTimeline.days[0]; row = await revealDay(day);
    row.querySelector(".timeline-photo").click(); await wait(() => state.timelineDay === day, "bad photo card");
    $("detail").querySelector(".tagctl .tg button").click();
    await wait(() => !photoTimeline.days.length && !state.timelineDay, "tag removes day");
    check("Tag edit removes a nonmatching photo", !$("veil").classList.contains("on"));

    $("timelineTagsClear").click(); change("timelineRating", "good");
    change("timelineUnrated", false); change("timelineLimit", true); await settled();
    const bulkDay = photoTimeline.days.find(day => day.day === "2019-01-01");
    row = await revealDay(bulkDay);
    row.querySelector(".timeline-add-tags").click();
    await wait(() => timelineBulk.preview, "bulk day preview");
    check("Bulk dialog counts filtered photos and includes hidden highlights", $("timelineBulk").open &&
      timelineBulk.preview.matched === 24 && timelineBulk.preview.total === 40 &&
      $("timelineBulkScope").textContent.includes("9 photos hidden") && !$("timelineBulkWarning").hidden &&
      $("timelineBulkWarning").textContent.includes("16 photos") && $("timelineBulkOK").disabled);
    const dialogRect = $("timelineBulk").getBoundingClientRect();
    check("Tag dialog fits the viewport", dialogRect.left >= 0 && dialogRect.right <= innerWidth &&
      dialogRect.top >= 0 && dialogRect.bottom <= innerHeight && $("timelineBulk").scrollWidth <= $("timelineBulk").clientWidth);
    $("timelineBulkSearch").value = "bad tag";
    $("timelineBulkSearch").dispatchEvent(new Event("input")); $("timelineBulkAdd").click();
    check("Invalid bulk tags are explained", !timelineBulk.selected.length && $("timelineBulkStatus").textContent.includes("letters and digits"));
    addTimelineBulkTag("cancelled"); await wait(() => timelineBulk.preview, "cancel preview");
    $("timelineBulkCancel").click();
    const bulkRecords = async () => (await fetch("/api/timeline/day?day=2019-01-01&rating_mode=all&unrated=1&limit=500").then(r => r.json())).items;
    check("Cancel leaves the whole day unchanged", !$("timelineBulk").open &&
      (await bulkRecords()).every(it => it.tags === "base"));
    row.querySelector(".timeline-add-tags").click();
    await wait(() => timelineBulk.preview && $("timelineBulkSuggestions").querySelector("button"), "bulk suggestions");
    $("timelineBulkSearch").value = "BASE";
    $("timelineBulkSearch").dispatchEvent(new Event("input"));
    await wait(() => $("timelineBulkSuggestions").querySelector("button")?.textContent.startsWith("base"), "search existing bulk tag");
    $("timelineBulkSuggestions").querySelector("button").click();
    await wait(() => timelineBulk.preview && timelineBulk.selected[0] === "base", "existing bulk tag");
    $("timelineBulkSearch").value = "NewBulk";
    $("timelineBulkSearch").dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", bubbles:true, cancelable:true}));
    await wait(() => timelineBulk.preview && !$("timelineBulkOK").disabled, "new bulk tag ready");
    check("Bulk selection combines existing and new normalized tags", timelineBulk.selected.join(",") === "base,newbulk" && !photoTimeline.tags.length);
    await fetch("/api/comment/3001", {method:"POST", body:new URLSearchParams({text:"Concurrent edit"})});
    $("timelineBulkOK").click();
    await wait(() => !timelineBulk.busy && $("timelineBulkStatus").textContent.includes("archive changed"), "bulk revision conflict");
    check("Concurrent edits require reviewing refreshed counts", $("timelineBulk").open && !$("timelineBulkOK").disabled &&
      (await bulkRecords()).every(it => it.tags === "base"));
    $("timelineBulkOK").click();
    await wait(() => !$("timelineBulk").open && !photoTimeline.updating.size, "bulk save"); await settled();
    const taggedBulk = await bulkRecords();
    check("Bulk save adds tags to all filtered photos beyond fifteen and preserves excluded photos", taggedBulk.every(it =>
      it.mark === "great" ? it.tags === "base,newbulk" : it.tags === "base") && taggedBulk.filter(it => it.tags.includes("newbulk")).length === 24);
    check("Bulk save invalidates Statistics and keeps Timeline filters", !state.statsLoaded && photoTimeline.ratingMode === "good" &&
      !photoTimeline.includeUnrated && photoTimeline.limitDaily && !photoTimeline.tags.length);
    const updatedBulk = photoTimeline.days.find(day => day.day === bulkDay.day);
    row = await revealDay(updatedBulk);
    check("Bulk save refreshes thumbnail tags and badges", [...row.photoNodes.values()].every(button =>
      button.title.includes("Tags: base, newbulk") && !button.querySelector(".timeline-tags-badge").hidden));
    row.querySelector(".timeline-toggle-all").click();
    await wait(() => updatedBulk.allNext === null && !updatedBulk.allLoading, "tagged day expanded");
    check("Show all contains all bulk-tagged matches", updatedBulk.allItems.length === 24 &&
      updatedBulk.allItems.every(it => tagsOf(it).includes("newbulk")));
    change("timelineLimit", false); await settled();
    row = await revealDay(photoTimeline.days.find(day => day.day === bulkDay.day));
    row.querySelector(".timeline-add-tags").click(); await wait(() => timelineBulk.preview, "unlimited bulk preview");
    check("Unlimited bulk preview uses the same matches without a display-limit note", timelineBulk.preview.matched === 24 &&
      !$("timelineBulkScope").textContent.includes("hidden by"));
    addTimelineBulkTag("second"); await wait(() => timelineBulk.preview && !$("timelineBulkOK").disabled, "second bulk tag");
    $("timelineBulkOK").click(); await wait(() => !$("timelineBulk").open && !photoTimeline.updating.size, "unlimited bulk save"); await settled();
    row = await revealDay(photoTimeline.days.find(day => day.day === bulkDay.day));
    row.querySelector(".timeline-add-tags").click(); await wait(() => timelineBulk.preview, "capacity preview");
    addTimelineBulkTag("third"); await wait(() => timelineBulk.preview, "capacity checked");
    check("Bulk tag capacity conflict blocks the entire operation", timelineBulk.preview.conflicts === 24 &&
      $("timelineBulkOK").disabled && $("timelineBulkStatus").textContent.includes("No photos will be changed"));
    $("timelineBulkCancel").click();
    change("timelineRating", "all"); change("timelineUnrated", true); await settled();
    row = await revealDay(photoTimeline.days.find(day => day.day === bulkDay.day));
    row.querySelector(".timeline-add-tags").click(); await wait(() => timelineBulk.preview, "whole day bulk preview");
    check("Whole-day tagging hides the filter warning", timelineBulk.preview.matched === 40 && $("timelineBulkWarning").hidden);
    $("timelineBulkCancel").click();

    // Removal offers only tags from the complete filtered day, and can remove
    // the current filter tag and other tags together without losing targets.
    await fetch("/api/tags/3001?tag=base&tag=newbulk&tag=unique", {method:"POST"});
    await fetch("/api/tags/3002?tag=base&tag=newbulk&tag=different", {method:"POST"});
    change("timelineRating", "good"); change("timelineUnrated", false); change("timelineLimit", true);
    selectTimelineTag("base", true); await settled();
    let removalDay = photoTimeline.days.find(day => day.day === bulkDay.day);
    row = await revealDay(removalDay);
    row.querySelector(".timeline-remove-tags").click(); await wait(() => timelineBulk.preview, "removal preview");
    check("Remove tags lists every tag in the filtered day with counts", timelineBulk.mode === "remove" &&
      $("timelineBulkTitle").textContent.startsWith("Remove tags") && $("timelineBulkAdd").hidden &&
      timelineBulk.available.length === 5 && $("timelineBulkSuggestions").querySelectorAll("input[type=checkbox]").length === 5 &&
      timelineBulk.available.find(item => item.tag === "second").n === 22 &&
      !timelineBulk.available.some(item => ["kids", "море"].includes(item.tag)));
    check("Removal warns about excluded photos and counts hidden matches", !$("timelineBulkWarning").hidden &&
      $("timelineBulkWarning").textContent.includes("16 photos") &&
      $("timelineBulkScope").textContent.includes("9 photos hidden") && $("timelineBulkOK").disabled);
    check("Date controls fit within their day", [...photoTimeline.mounted.values()].every(section =>
      section.querySelector(".timeline-date").getBoundingClientRect().height <= section.getBoundingClientRect().height));
    const removeCheck = tag => [...$("timelineBulkSuggestions").querySelectorAll("input")].find(input => input.value === tag);
    const removeChoose = async tag => {
      removeCheck(tag).focus();
      removeCheck(tag).click(); await wait(() => timelineBulk.preview, "removal selection " + tag);
    };
    $("timelineBulkSearch").value = "UNIQUE"; $("timelineBulkSearch").dispatchEvent(new Event("input"));
    check("Removal tag search filters the day list", $("timelineBulkSuggestions").querySelectorAll("input").length === 1 && !!removeCheck("unique"));
    await removeChoose("unique");
    check("Removal keeps keyboard focus after updating counts", document.activeElement === removeCheck("unique"));
    $("timelineBulkSearch").value = ""; $("timelineBulkSearch").dispatchEvent(new Event("input"));
    check("Removal search preserves checked tags", removeCheck("unique").checked && !$("timelineBulkOK").disabled);
    $("timelineBulkCancel").click();
    check("Cancelling removal preserves tags", (await bulkRecords()).find(it => it.id === 3001).tags.includes("unique"));
    row.querySelector(".timeline-remove-tags").click(); await wait(() => timelineBulk.preview, "removal reopen");
    await removeChoose("base"); await removeChoose("newbulk"); await removeChoose("unique"); await removeChoose("different");
    check("Removal supports more than three selected tags", timelineBulk.selected.length === 4 &&
      !$("timelineBulkSearch").disabled && !$("timelineBulkOK").disabled && timelineBulk.preview.changed === 24);
    await fetch("/api/comment/3001", {method:"POST", body:new URLSearchParams({text:"Concurrent removal edit"})});
    $("timelineBulkOK").click();
    await wait(() => !timelineBulk.busy && $("timelineBulkStatus").textContent.includes("archive changed"), "removal revision conflict");
    check("Removal refreshes the tag list after a concurrent edit", $("timelineBulk").open &&
      timelineBulk.available.length === 5 && (await bulkRecords()).filter(it => it.mark === "great").every(it => tagsOf(it).includes("base")));
    $("timelineBulkOK").click();
    await wait(() => !$("timelineBulk").open && !photoTimeline.updating.size, "filtered removal saved"); await settled();
    const removedBulk = await bulkRecords();
    check("Removal includes hidden photos, preserves excluded photos and keeps other tags", removedBulk.every(it =>
      it.mark === "great" ? (it.id <= 3002 ? !it.tags : it.tags === "second") : it.tags === "base"));
    check("Removing the filter tag updates Timeline membership without clearing filters", photoTimeline.tags.join(",") === "base" &&
      !photoTimeline.days.some(day => day.day === bulkDay.day) && !state.statsLoaded);
    $("timelineTagsClear").click(); change("timelineLimit", false); await settled();
    removalDay = photoTimeline.days.find(day => day.day === bulkDay.day); row = await revealDay(removalDay);
    row.querySelector(".timeline-remove-tags").click(); await wait(() => timelineBulk.preview, "unlimited removal preview");
    check("Unlimited removal lists tags only on matching photos", timelineBulk.available.length === 1 &&
      timelineBulk.available[0].tag === "second" && timelineBulk.available[0].n === 22 && !$("timelineBulkScope").textContent.includes("hidden by"));
    await removeChoose("second"); $("timelineBulkOK").click();
    await wait(() => !$("timelineBulk").open && !photoTimeline.updating.size, "unlimited removal saved"); await settled();
    removalDay = photoTimeline.days.find(day => day.day === bulkDay.day); row = await revealDay(removalDay);
    check("Removal refreshes badges and tooltips on matching photos", [...row.photoNodes.values()].every(button =>
      button.querySelector(".timeline-tags-badge").hidden && !button.title.includes("\nTags:")));
    row.querySelector(".timeline-remove-tags").click(); await wait(() => timelineBulk.preview, "empty removal list");
    check("Untagged matches show an empty state and cannot submit", !timelineBulk.available.length &&
      $("timelineBulkStatus").textContent === "No tags on matching photos." && $("timelineBulkOK").disabled);
    $("timelineBulkCancel").click();

    $("timelineTagsClear").click(); change("timelineRating", "great"); change("timelineUnrated", false);
    change("timelineLimit", true); await settled();
    selectTimelineTag("kids", true); await settled();
    await field("year"); await chooseOption("2020");
    day = photoTimeline.days.find(day => day.day === "2020-01-03"); row = await revealDay(day);
    row.querySelector(".timeline-date .tool").click();
    await wait(() => state.page === "statistics", "Analyse day");
    check("Analyse day opens only the whole-day Statistics filter", JSON.stringify(state.filters) === JSON.stringify({day:[day.day]}));
    history.back(); await wait(() => state.page === "timeline", "Back to Timeline");
    check("Back retains current in-memory Timeline options", photoTimeline.ratingMode === "great" &&
      !photoTimeline.includeUnrated && photoTimeline.limitDaily && photoTimeline.tags[0] === "kids" &&
      photoTimeline.extraType === "year" && photoTimeline.extraValue === "2020");
    check("Browser has no script errors", !window.timelineTestErrors.length);
    sessionStorage.setItem("timeline-test-checks", JSON.stringify(checks));
    sessionStorage.setItem("timeline-test-reload", "1");
    location.reload();
  } catch (error){ finish({ok:false, checks, error:String(error), errors:window.timelineTestErrors}); }
})();
</script>
"""


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


class DevTools:
    """Small standard-library WebSocket transport for local Chromium test hosts.

    Only protocol commands used below are exposed; no external browser service
    or additional Python runtime dependencies are needed in CI.
    """

    def __init__(self, url):
        parsed = urllib.parse.urlsplit(url)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
        self.buffer = bytearray()
        self.serial = 0
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.socket.sendall((f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
                             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                             f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buffer:
            self.buffer.extend(self.socket.recv(4096))
        headers, remaining = self.buffer.split(b"\r\n\r\n", 1)
        if b" 101 " not in headers.split(b"\r\n", 1)[0]:
            raise RuntimeError("Chromium rejected its local DevTools connection")
        self.buffer = bytearray(remaining)

    def read(self, size):
        while len(self.buffer) < size:
            data = self.socket.recv(max(4096, size - len(self.buffer)))
            if not data:
                raise EOFError("Chromium closed DevTools")
            self.buffer.extend(data)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def send(self, payload, opcode=1):
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytes([0x80 | opcode, 0x80 | (length if length < 126 else 126 if length < 65536 else 127)])
        if length >= 126:
            header += struct.pack("!H" if length < 65536 else "!Q", length)
        self.socket.sendall(header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))

    def receive(self):
        parts = bytearray()
        while True:
            first, second = self.read(2)
            size = second & 127
            if size in (126, 127):
                size = struct.unpack("!H" if size == 126 else "!Q", self.read(2 if size == 126 else 8))[0]
            payload = self.read(size)
            opcode = first & 15
            if opcode == 8:
                raise EOFError("Chromium closed DevTools")
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode in (0, 1):
                parts.extend(payload)
                if first & 0x80:
                    return json.loads(parts)

    def call(self, method, **params):
        self.serial += 1
        self.send(json.dumps({"id": self.serial, "method": method, "params": params}).encode())
        while True:
            response = self.receive()
            if response.get("id") == self.serial:
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response.get("result", {})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--no-sandbox", action="store_true")
    parser.add_argument("--allow-skip", action="store_true")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    candidates = [args.browser, shutil.which("google-chrome"), shutil.which("chromium"),
                  Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
                  Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe"]
    browser = next((Path(path) for path in candidates if path and Path(path).is_file()), None)
    if not browser:
        if args.allow_skip:
            print("SKIP: no installed Chromium browser")
            return 0
        parser.error("No installed Chromium browser; pass --browser")
    with tempfile.TemporaryDirectory(prefix="photostats-timeline-browser-") as folder:
        app.DB_PATH = str(Path(folder) / "fixture.db")
        app.CACHE_DIR = str(Path(folder) / "thumbs")
        make_fixture(app.DB_PATH)
        with closing(sqlite3.connect(app.DB_PATH)) as con, con:
            # Cross rating/page boundaries in both directions and include ties;
            # the transport below also deliberately reverses server batches.
            con.execute("UPDATE photos SET taken = substr(taken,1,10) || ' ' || "
                        "CASE WHEN id % 5 = 0 THEN '12:00:00' ELSE "
                        "printf('%02d:%02d:%02d', (id*137/3600)%24, (id*137/60)%60, (id*137)%60) END")
            con.execute("UPDATE photos SET iso=400, shutter='1/125'")
            con.executemany("INSERT INTO photos(id,path,filename,ext,size,taken,year,month,ym,mark,camera,lens) "
                            "VALUES (?,?,?,'jpg',1000,?,2019,1,'2019-01',?,'Camera 00','Lens 00')",
                            [(3001+i, f"bulk/{i}.jpg", f"bulk-{i}.jpg", f"2019-01-01 12:00:{i:02d}",
                              "great" if i < 24 else "bad" if i < 32 else None) for i in range(40)])
            con.executemany("INSERT INTO photo_tags VALUES (?,'base')", [(3001+i,) for i in range(40)])
        picture = io.BytesIO()
        Image.new("RGB", (8, 8), (104, 118, 91)).save(picture, "JPEG")

        @app.app.before_request
        def thumbnail():
            if request.path.startswith("/api/thumb/"):
                return Response(picture.getvalue(), mimetype="image/jpeg")

        @app.app.after_request
        def unordered_timeline(response):
            if request.path in ("/api/timeline/photos", "/api/timeline/day") and response.status_code == 200:
                data = response.get_json()
                for day in data.get("days", []):
                    day["items"].reverse()
                if "items" in data:
                    data["items"].reverse()
                response.set_data(json.dumps(data))
            return response

        @app.app.get("/__timeline_test")
        def harness():
            source = (Path(app.APP_DIR) / "web/index.html").read_text(encoding="utf-8")
            early = '<script>window.timelineTestErrors=[];window.addEventListener("error",e=>window.timelineTestErrors.push(e.message));</script>'
            source = source.replace("<script>", early + "<script>", 1)
            return Response(source.replace("</body>", SCRIPT + "</body>"), mimetype="text/html")

        server = make_server("127.0.0.1", 0, app.app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            command = [str(browser), "--headless", "--disable-gpu", "--in-process-gpu",
                       "--disable-software-rasterizer", "--disable-background-networking",
                       "--disable-component-update", "--disable-sync", "--no-first-run",
                       "--no-default-browser-check", f"--user-data-dir={Path(folder) / 'profile'}",
                       f"--window-size={args.width},900", "--remote-debugging-port=0"]
            if args.no_sandbox:
                command.append("--no-sandbox")
            command.append("about:blank")
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen(command, stdout=log, stderr=log,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                client = None
                try:
                    port_file = Path(folder) / "profile/DevToolsActivePort"
                    deadline = time.monotonic() + 45
                    while True:
                        try:
                            # Chromium can create the file before releasing its
                            # Windows write lock or finishing the first line.
                            port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
                            if not 0 < port < 65536:
                                raise ValueError("Invalid DevTools port")
                            break
                        except (OSError, ValueError, IndexError):
                            pass
                        if process.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            raise RuntimeError("Chromium did not start: " + log.read().decode(errors="replace")[-1500:])
                        time.sleep(0.05)
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                        pages = json.load(response)
                    page = next(page for page in pages if page["type"] == "page")
                    client = DevTools(page["webSocketDebuggerUrl"])
                    # New headless Chrome may clamp window sizes below 500 px;
                    # emulate the CSS viewport explicitly for the 360 px check.
                    client.call("Emulation.setDeviceMetricsOverride", width=args.width, height=900,
                                deviceScaleFactor=1, mobile=False)
                    client.call("Page.navigate", url=f"http://127.0.0.1:{server.server_port}/__timeline_test#timeline")
                    while time.monotonic() < deadline:
                        result = client.call("Runtime.evaluate", expression="document.getElementById('timelineTestResult')?.textContent",
                                             returnByValue=True)
                        value = result.get("result", {}).get("value")
                        if value:
                            report = json.loads(value)
                            if args.screenshot:
                                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                                shot = client.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
                                args.screenshot.write_bytes(base64.b64decode(shot["data"]))
                            print(json.dumps(report, indent=2, ensure_ascii=True))
                            return int(not report["ok"] or bool(report["errors"]) or report["width"] != args.width)
                        time.sleep(0.05)
                    raise TimeoutError("Timeline browser checks did not finish")
                finally:
                    if client:
                        try:
                            client.call("Browser.close")
                        except (OSError, EOFError):
                            pass
                        client.socket.close()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=5)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    sys.exit(main())
