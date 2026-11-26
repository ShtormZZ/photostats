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
import raw_pairs
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
    !photoTimeline.error && !photoTimeline.updating.size && !photoTimeline.layoutFrame &&
    !photoTimeline.days.some(d => d.allLoading), "Timeline settled");
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

    const selection = await fetch("/api/photos?limit=30").then(r => r.json());
    check("Selection returns JPGs with RAW companions and no paired RAW rows",
      selection.items.some(it => it.has_raw) && selection.items.every(it => it.ext === "jpg"));
    renderStrip(selection.items); renderList(selection, false);
    check("Selection strip and list show RAW badges only for paired JPGs",
      [...$("strip").querySelectorAll(".raw-badge")].every((badge, i) => badge.hidden === !selection.items[i].has_raw) &&
      [...$("list").querySelectorAll(".raw-badge")].every((badge, i) => badge.hidden === !selection.items[i].has_raw));
    const pairedSelection = selection.items.find(it => it.has_raw);
    openDetail(pairedSelection, selection.items);
    check("Photo card shows a RAW badge", !$("detail").querySelector("h3 .raw-badge").hidden);
    const rawPaths = () => [...$("detail").querySelectorAll(".raw-path")].map(node => node.textContent);
    check("Selection card shows companion paths and a reveal button per RAW",
      pairedSelection.raw_files.length > 0 &&
      JSON.stringify(rawPaths()) === JSON.stringify(pairedSelection.raw_files.map(file => file.path)) &&
      $("detail").querySelectorAll(".raw-reveal").length === pairedSelection.raw_files.length &&
      $("detail").querySelector(".raw-reveal").textContent === "Show RAW in Explorer");
    const rawFetch = window.fetch.bind(window);
    let rawRequest;
    window.fetch = (url, options) => {
      if (String(url).startsWith("/api/reveal-raw/")){
        rawRequest = {url:String(url), method:options?.method};
        return Promise.resolve(new Response(JSON.stringify({ok:true}), {headers:{"Content-Type":"application/json"}}));
      }
      return rawFetch(url, options);
    };
    $("detail").querySelector(".raw-reveal").click();
    await wait(() => rawRequest, "RAW reveal request"); window.fetch = rawFetch;
    check("Show RAW in Explorer sends the JPEG and RAW IDs",
      rawRequest.url === "/api/reveal-raw/" + pairedSelection.id + "/" + pairedSelection.raw_files[0].id &&
      rawRequest.method === "POST");
    const filesAllowed = state.files;
    state.files = false; openDetail(pairedSelection, selection.items);
    check("Network mode keeps RAW paths and hides file actions", rawPaths().length > 0 &&
      !$("detail").querySelector(".raw-reveal"));
    state.files = filesAllowed;
    const unpairedSelection = selection.items.find(it => !it.has_raw);
    openDetail(unpairedSelection, selection.items);
    check("Cards without RAW omit both the path block and reveal button", !$("detail").querySelector(".raw-files"));
    openDetail({...pairedSelection, raw_files:[{...pairedSelection.raw_files[0],
      path:"C:/Archive/Съёмка & <b>literal</b>/" + "long-path-".repeat(20) + ".nef"}]}, selection.items);
    const rawPath = $("detail").querySelector(".raw-path");
    check("RAW paths render as plain text and wrap inside the card", rawPath.textContent.includes("<b>literal</b>") &&
      !rawPath.querySelector("b") && rawPath.scrollWidth <= rawPath.clientWidth + 1);
    closeDetail();
    const pairedRow = [...$("list").querySelectorAll(".lrow")][selection.items.indexOf(pairedSelection)];
    pairedRow.click();
    check("Expanded Selection information also shows RAW paths and actions", pairedRow.nextElementSibling.querySelectorAll(".raw-path").length ===
      pairedSelection.raw_files.length && pairedRow.nextElementSibling.querySelector(".raw-reveal"));

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
      previewButton.title.split("\n").length === 4 + Number(!!previewPhoto.has_raw) + Number(!!previewPhoto.lens) + Number(tagsOf(previewPhoto).length > 0) + Number(!!previewPhoto.comment) &&
      previewButton.title.includes("Camera model: ") && previewButton.title.includes("\nLens: ") === !!previewPhoto.lens &&
      previewButton.title.includes("Focal length: ") && previewButton.title.includes(" · Aperture: ") &&
      previewButton.title.includes(" · ISO: ") && previewButton.title.includes(" · Shutter speed: ") &&
      previewButton.title.includes("\nTags: ") === (tagsOf(previewPhoto).length > 0) &&
      previewButton.title.includes("\nComment: ") === !!previewPhoto.comment);
    check("Timeline badges and tooltips report RAW companions", [...row.photoNodes.values()].every(button => {
      const photo = large.items.find(it => String(it.id) === button.dataset.pid);
      return button.querySelector(".raw-badge").hidden === !photo.has_raw &&
        button.title.includes("RAW file available") === !!photo.has_raw;
    }));
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
    const pairedTimeline = large.allItems.find(it => it.has_raw);
    await openTimelinePhoto(large, pairedTimeline.id);
    check("Timeline cards show RAW paths and actions", state.shown.raw_files.length > 0 &&
      JSON.stringify(rawPaths()) === JSON.stringify(state.shown.raw_files.map(file => file.path)) &&
      $("detail").querySelectorAll(".raw-reveal").length === state.shown.raw_files.length);
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
    const lines = thumb().title.split("\n").filter(line => line !== "RAW file available");
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
      !thumb().title.includes("\nTags: ") && !thumb().title.includes("\nComment: ") &&
      thumb().title.split("\n").length === 5 + Number(!!card.has_raw));
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
    const equivalentPhotos = photoTimeline.days.flatMap(day => day.items);
    check("Focal filter and client matching use 35mm equivalents", equivalentPhotos.length > 0 &&
      equivalentPhotos.every(it => it.focal35 >= 36 && it.focal35 < 51 && matchesTimelinePhoto(it)));
    check("Equivalent ranges include photos outside the actual focal range", equivalentPhotos.some(it =>
      it.focal === null || it.focal < 36 || it.focal >= 51));
    await field("focal"); await chooseOption(NONE);
    const missingEquivalentPhotos = photoTimeline.days.flatMap(day => day.items);
    check("Missing equivalents stay unspecified in server and client filters", missingEquivalentPhotos.length > 0 &&
      missingEquivalentPhotos.every(it => it.focal35 === null && matchesTimelinePhoto(it)) &&
      missingEquivalentPhotos.some(it => it.focal !== null));
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

    change("timelineRating", "all"); change("timelineUnrated", true); await settled();
    const portfolioDay = photoTimeline.days.find(day => day.day === "2020-01-03");
    row = await revealDay(portfolioDay);
    await wait(() => !thumbBusy && !thumbQ.length, "Timeline thumbnail queue idle before switching views");
    const timelineTileWidth = row.querySelector(".timeline-photo[data-pid]").getBoundingClientRect().width;
    const portfolioOptions = () => ["portfolioHideBorders", "portfolioHideDates"].map($);
    check("The Portfolio toggles are hidden in Timeline", portfolioOptions().every(input => !input.getClientRects().length));
    change("timelineView", "portfolio"); await settled();
    row = await revealDay(portfolioDay);
    await wait(() => !thumbBusy && !thumbQ.length && !photoTimeline.layoutFrame, "Portfolio thumbnails finished loading");
    const portfolioThumbnails = [...$("timelineStage").querySelectorAll(".timeline-photo[data-pid]")];
    check("Portfolio loads its thumbnails when switching views with an idle queue", portfolioThumbnails.length > 0 &&
      portfolioThumbnails.every(button => button.style.backgroundImage && getComputedStyle(button.querySelector(".noimg")).display === "none"));
    const portfolioOrder = items => [items.filter(it => effRating(it) === "great"),
      items.filter(it => effRating(it) === "5"),
      items.filter(it => !["great", "5"].includes(effRating(it)))].flatMap(byTime);
    const inPortfolioOrder = items => JSON.stringify(ids(items)) === JSON.stringify(ids(portfolioOrder(items)));
    const spanOverlap = (a, sizeA, b, sizeB) => Math.min(a + sizeA, b + sizeB) - Math.max(a, b);
    const tileNeighbors = (a, b, gap) =>
      ((Math.abs(a.left + a.width + gap - b.left) < .01 || Math.abs(b.left + b.width + gap - a.left) < .01) &&
        spanOverlap(a.top, a.height, b.top, b.height) > .001) ||
      ((Math.abs(a.top + a.height + gap - b.top) < .01 || Math.abs(b.top + b.height + gap - a.top) < .01) &&
        spanOverlap(a.left, a.width, b.left, b.width) > .001);
    const connectedTiles = (boxes, gap, allowCorners = false) => {
      if (!boxes.length) return true;
      const axisTouches = (startA, endA, startB, endB) =>
        Math.abs(startA - endB - gap) < .01 || Math.abs(startB - endA - gap) < .01;
      const cornerNeighbors = (a, b) => axisTouches(a.left, a.left + a.width, b.left, b.left + b.width) &&
        axisTouches(a.top, a.top + a.height, b.top, b.top + b.height);
      const reached = new Set([boxes[0]]);
      for (const box of reached){
        for (const other of boxes){
          if (!reached.has(other) && (tileNeighbors(box, other, gap) || (allowCorners && cornerNeighbors(box, other))))
            reached.add(other);
        }
      }
      return reached.size === boxes.length;
    };
    const mosaicHasConnectedDays = (layout, entries, showDates = true) => {
      let previous;
      for (let start = 0; start < entries.length;){
        const first = layout.boxes[start], day = entries[start].portfolioDay;
        if (!!entries[start].portfolioDate !== showDates || !first || (previous && (first.top < previous.top - .01 ||
            (Math.abs(first.top - previous.top) < .01 && first.left <= previous.left)))) return false;
        let end = start + 1;
        while (entries[end]?.portfolioDay === day) end++;
        const tiles = layout.boxes.slice(start, end);
        if (!connectedTiles(tiles, layout.gap, !showDates) || (showDates && tiles[1] && !tileNeighbors(first, tiles[1], layout.gap)) ||
            !tiles.every((b, i) => !i || b.top > tiles[i-1].top ||
              (b.top === tiles[i-1].top && b.left > tiles[i-1].left))) return false;
        previous = first; start = end;
      }
      return true;
    };
    check("Portfolio switches in place and retains filters", photoTimeline.view === "portfolio" &&
      photoTimeline.mounted.has(portfolioDay.day) && photoTimeline.ratingMode === "all" && photoTimeline.includeUnrated);
    check("Portfolio sorts three distinct groups on the client", inPortfolioOrder(portfolioDay.allItems) &&
      portfolioDay.allItems.some(it => effRating(it) === "5") && portfolioDay.allItems.some(it => effRating(it) === "bad"));
    check("Portfolio uses date tiles without day headers or actions", $("timelineStage").clientWidth >= innerWidth - 40 &&
      row.querySelector(".portfolio-date-tile") && !$("timelineStage").querySelector(".timeline-date,.timeline-actions,button.tool"));
    check("Portfolio shows both toggles with dates and borders enabled by default",
      portfolioOptions().every(input => input.getClientRects().length && !input.checked));
    const ratedDays = Array.from({length:5}, (_, i) => {
      const day = {day:"2020-01-0" + (5 - i)};
      return [{portfolioDate:day.day, portfolioDay:day}, ...Array.from({length:i + 2}, (_, j) => ({
        portfolioDay:day, mark:j === 0 ? "great" : "5", width:j % 2 ? 4000 : 6000, height:j % 2 ? 6000 : 4000}))];
    }).flat();
    check("Multiple days with mostly large images each form a connected region", [109, 360, 1280, 3840].every(width =>
      mosaicHasConnectedDays(portfolioMosaic(ratedDays, width), ratedDays)));
    const markerlessDays = ratedDays.filter(entry => !entry.portfolioDate);
    check("Portfolio packs markerless days as separate connected regions across widths", [109, 360, 1280, 3840].every(width =>
      mosaicHasConnectedDays(portfolioMosaic(markerlessDays, width, true), markerlessDays, false)));
    const plainDay = {day:"2020-01-01"}, plainPhotos = Array.from({length:4}, () => ({portfolioDay:plainDay}));
    const withDate = portfolioMosaic([{portfolioDate:plainDay.day, portfolioDay:plainDay}, ...plainPhotos], 360);
    const withoutDate = portfolioMosaic(plainPhotos, 360);
    check("Removing a date frees its cell for the first photo and can shorten the mosaic",
      JSON.stringify(withDate.boxes[0]) === JSON.stringify(withoutDate.boxes[0]) && withDate.height > withoutDate.height);
    check("Portfolio has no photo badges", !row.querySelector(".raw-badge,.timeline-badge,.timeline-annotations"));
    const portfolioLayout = photoTimeline.portfolioStream;
    const fittedColumns = Math.max(4, Math.round((portfolioLayout.width + portfolioLayout.gap) /
      (timelineTileWidth + portfolioLayout.gap)));
    const ordinaryTile = portfolioLayout.entries.findIndex(entry => entry.portfolioIndex >= 0 && !portfolioFeatured(entry));
    check("Portfolio fits whole columns across the stage at the Timeline scale", ordinaryTile >= 0 &&
      Math.abs(fittedColumns * (portfolioLayout.base + portfolioLayout.gap) - portfolioLayout.gap - portfolioLayout.width) < .01 &&
      Math.abs(portfolioLayout.boxes[ordinaryTile].width - portfolioLayout.base) < .01);
    const rightEdgeFilled = () => {
      const stage = $("timelineStage"), right = stage.getBoundingClientRect().right;
      return [...stage.querySelectorAll(".portfolio-tile,.portfolio-date-tile,.portfolio-filler")]
        .some(tile => Math.abs(tile.getBoundingClientRect().right - right) < .05);
    };
    check("Mounted Portfolio tiles reach the right edge", rightEdgeFilled());
    const alignedToGrid = (layout, box) => {
      const pitch = layout.base + layout.gap;
      return [box.left, box.top, box.width + layout.gap, box.height + layout.gap].every(value =>
        Math.abs(value / pitch - Math.round(value / pitch)) < .001);
    };
    const tileSpans = (layout, box, columns, rows) => {
      const pitch = layout.base + layout.gap;
      return Math.abs(box.width + layout.gap - columns * pitch) < .01 &&
        Math.abs(box.height + layout.gap - rows * pitch) < .01;
    };
    check("Portfolio mosaic stays within the width without overlapping images or captions", portfolioLayout.rows.every(line => {
      const boxes = line.indices.map(i => portfolioLayout.boxes[i]);
      return boxes.every((box, i) => alignedToGrid(portfolioLayout, box) && box.left >= 0 && box.left + box.width <= portfolioLayout.width + .01 &&
        boxes.slice(i + 1).every(other => box.left + box.width <= other.left + .01 ||
          other.left + other.width <= box.left + .01 || box.top + box.height <= other.top + .01 ||
          other.top + other.height <= box.top + .01));
    }));
    check("All mounted photos belong to their connected day and have unique nodes", mosaicHasConnectedDays(portfolioLayout, portfolioLayout.entries) &&
      new Set(portfolioThumbnails.map(button => button.dataset.pid)).size === portfolioThumbnails.length);
    check("All Portfolio thumbnails cover their tile with a centred crop", portfolioThumbnails.every(button => {
      const style = getComputedStyle(button);
      return style.backgroundSize === "cover" && style.backgroundPosition === "50% 50%";
    }));
    const outline = portfolioDayOutline([{left:0, top:0, width:40, height:40}, {left:46, top:0, width:86, height:86}], 6);
    check("An outline traces the outside through the gutter and cancels the common edge", outline.some(edge =>
      edge[0] === 43 && edge[1] === 43 && edge[2] === 43 && edge[3] === 89) &&
      !outline.some(edge => edge[0] === 43 && edge[2] === 43 && edge[1] < 43));
    const outlineCanvas = $("portfolioOutlines");
    check("Portfolio paints a viewport-sized, noninteractive day outline layer", outlineCanvas?.getAttribute("aria-hidden") === "true" &&
      getComputedStyle(outlineCanvas).pointerEvents === "none" && parseFloat(outlineCanvas.style.height) <= innerHeight + 1 &&
      outlineCanvas.getContext("2d").getImageData(0, 0, outlineCanvas.width, outlineCanvas.height).data.some((value, i) => i % 4 === 3 && value));
    check("Portfolio colors the full gutter between days and beside fillers", (() => {
      const boxes = [{left:0, top:0, width:40, height:40}, {left:46, top:0, width:40, height:86}];
      const regions = new Map([["A", {start:0, end:1, color:"#ff0000"}], ["B", {start:1, end:2, color:"#0000ff"}]]);
      renderPortfolioOutlines($("timelineStage"), {width:86, height:86, gap:6, boxes, regions}, 0, new Map([["A",[0]], ["B",[1]]]));
      const ctx = outlineCanvas.getContext("2d"), scale = ctx.getTransform().a;
      const pixel = (x, y) => [...ctx.getImageData(Math.floor((x + 3) * scale), Math.floor(y * scale), 1, 1).data].join();
      const samples = Array.from({length:6}, (_, i) => i + 40.5);
      const ok = samples.every(x => pixel(x, 20) === (x < 43 ? "255,0,0,255" : "0,0,255,255")) &&
        samples.every(y => pixel(20, y) === "255,0,0,255") && pixel(20, 20) === "0,0,0,0";
      renderPortfolioStream();
      return ok;
    })());
    check("Portfolio gives diagonally connected days a continuous colored gutter at the corner", (() => {
      const boxes = [[0,0], [46,46], [46,0], [0,46]].map(([left, top]) => ({left, top, width:40, height:40}));
      const regions = new Map([["A", {start:0, end:2, color:"#ff0000"}], ["B", {start:2, end:4, color:"#0000ff"}]]);
      renderPortfolioOutlines($("timelineStage"), {width:86, height:86, gap:6, boxes, regions}, 0, new Map([["A",[0,1]], ["B",[2,3]]]));
      const ctx = outlineCanvas.getContext("2d"), scale = ctx.getTransform().a;
      const pixel = (x, y) => [...ctx.getImageData(Math.floor((x + 3) * scale), Math.floor(y * scale), 1, 1).data].join();
      const ok = pixel(42.5, 42.5) === "255,0,0,255" && pixel(43.5, 43.5) === "255,0,0,255" &&
        pixel(43.5, 42.5) === "0,0,255,255" && pixel(42.5, 43.5) === "0,0,255,255";
      renderPortfolioStream();
      return ok;
    })());
    const stage = $("timelineStage");
    const hasDateTiles = () => !!stage.querySelector(".portfolio-date-tile");
    const datePhoto = portfolioDay.allItems[0];
    const photoButton = () => photoTimeline.mounted.get(portfolioDay.day)?.photoNodes.get(0);
    const dateInTitle = () => photoButton()?.title.split("\n").includes(datePhoto.taken);
    change("portfolioHideBorders", true);
    check("Hiding day borders keeps the date tiles and photo dates visible",
      !$("portfolioOutlines") && hasDateTiles() && dateInTitle() && photoTimeline.portfolioStream === portfolioLayout);
    change("portfolioHideDates", true);
    const markerless = photoTimeline.portfolioStream;
    renderPortfolioStream();
    check("Hiding dates repacks the same photos with no date boxes", markerless !== portfolioLayout &&
      markerless.entries.length === portfolioLayout.entries.length - portfolioLayout.dayStarts.size &&
      [...markerless.photos.keys()].join() === [...portfolioLayout.photos.keys()].join() &&
      mosaicHasConnectedDays(markerless, markerless.entries, false));
    check("Both switches apply to mounted tiles and subsequent renders",
      !hasDateTiles() && !$("portfolioOutlines") && photoButton() && !dateInTitle() &&
      !photoButton().getAttribute("aria-label").split("\n").includes(datePhoto.taken) && !row.hasAttribute("aria-label"));
    change("portfolioHideBorders", false);
    check("Restoring borders leaves dates hidden and keeps the same photo positions",
      !!$("portfolioOutlines") && !hasDateTiles() && !dateInTitle() &&
      photoTimeline.portfolioStream === markerless);
    await openTimelinePhoto(portfolioDay, datePhoto.id);
    const dateMetadata = () => [...$("detail").querySelectorAll(".capture-date")].some(node => node.getClientRects().length);
    check("Photo details also hide the capture date", $("veil").classList.contains("on") && !dateMetadata());
    change("portfolioHideDates", false);
    check("Restoring dates brings back the date tiles, tooltips and visible photo metadata",
      hasDateTiles() && dateInTitle() && dateMetadata() && !!$("portfolioOutlines"));
    closeDetail();
    const fillerFixture = {base:40, gap:6, width:461, rows:[{top:0, height:132, indices:[0, 1, 2, 3]}],
      entries:[{color_hex:"#FF0000", camera:"Test camera"}, {color_hex:"#0000FF"}, {portfolioDate:"2020-01-01"}, {color_hex:null, iso:400}],
      boxes:[{left:0, top:0, width:132, height:132}, {left:184, top:0, width:132, height:132},
        {left:322, top:0, width:40, height:40}, {left:368, top:0, width:40, height:40}]};
    const fillerSquares = portfolioFillers(fillerFixture, 0);
    const squareAt = (left, top) => fillerSquares.find(tile => tile.left === left && tile.top === top);
    check("Portfolio facts point to an adjoining photo with data, including large or uncolored photos",
      [0, 46, 92].every(top => squareAt(138, top)?.fact?.text === "Camera: Test camera" &&
        squareAt(138, top).fact.x === -1 && squareAt(138, top).fact.y === 0) &&
      squareAt(414, 0)?.fact?.text === "ISO 400" && !squareAt(414, 92)?.fact);
    const metadata = {tags:"trees,summer", rating:5, mark:"great", camera:"C", lens:"L", focal:35, shutter:"1/125", iso:200};
    check("Portfolio fact choices respect the active filter and effective rating",
      ["Tags: trees, summer", "Rating: 5+", "Camera: C", "Lens: L", "35 mm · 1/125", "ISO 200"]
        .every(fact => portfolioPhotoFacts(metadata, "").includes(fact)) &&
      [["camera","Camera: C"], ["lens","Lens: L"], ["focal","35 mm · 1/125"]].every(([field, fact]) =>
        !portfolioPhotoFacts(metadata, field).includes(fact)) && !portfolioPhotoFacts({}, "").length);
    check("Portfolio mixes equally close red and blue photos in every row of an interior gap", [0, 46, 92].every(top =>
      squareAt(138, top)?.color === "rgb(128,0,128)"));
    check("Portfolio fills the right of the final band with gray squares beyond the colored neighbors", [0, 46, 92].every(top =>
      squareAt(414, top)?.color === "#808080") && fillerSquares.length === 10 && fillerSquares.every(tile =>
        tile.width === 40 && tile.height === 40 && tile.left + tile.width <= fillerFixture.width));
    // The initial viewport can be packed completely, especially on narrow screens.
    const coloredRow = portfolioLayout.rows.find((line, i) =>
      (line.fillers ?? portfolioFillers(portfolioLayout, i)).some(tile => tile.color !== "#808080"));
    check("Stored photo colors reach the Portfolio mosaic", !!coloredRow);
    scrollTo(0, scrollY + $("timelineStage").getBoundingClientRect().top + coloredRow.top); schedulePhotoTimeline();
    const mountedFillers = () => [...$("timelineStage").querySelectorAll(".portfolio-filler")];
    await wait(() => mountedFillers().some(tile => tile.style.backgroundColor !== "rgb(128, 128, 128)"), "colored fillers mounted");
    const fillerLayer = $("portfolioFillers");
    check("Portfolio renders noninteractive square cards with arrows for displayed facts", !fillerLayer.hasAttribute("aria-hidden") &&
      getComputedStyle(fillerLayer).pointerEvents === "none" && mountedFillers().every(tile => {
        const box = Object.fromEntries(["left", "top", "width", "height"].map(key => [key, parseFloat(tile.style[key])]));
        const orange = "rgb(227, 155, 51)";
        return (!tile.textContent || [".arrow", ".fact"].every(selector => {
          const element = tile.querySelector(selector);
          return element?.textContent && getComputedStyle(element).color === orange;
        })) &&
          tileSpans(portfolioLayout, box, 1, 1) && alignedToGrid(portfolioLayout, box);
      }));
    row = await revealDay(portfolioDay);
    check("Stored sharpness bands select square tiles for 5+ and five-star photos", [25, 26].every(id => {
      const index = portfolioDay.allItems.findIndex(item => item.id === id);
      return index >= 0 && tileSpans(portfolioLayout, portfolioDay.portfolioLayout.boxes[index], 4, 4);
    }));
    const rotatedPhoto = {id:-1, mark:"great", width:6000, height:4000};
    portfolioThumbnailLoaded(rotatedPhoto, {naturalWidth:400, naturalHeight:600});
    check("Transposed thumbnails select the portrait grid size", portfolioTileSize(rotatedPhoto).width === 3 &&
      portfolioTileSize(rotatedPhoto).height === 4);
    const unspecifiedPhoto = {id:-2, mark:"5"};
    portfolioThumbnailLoaded(unspecifiedPhoto, {naturalWidth:800, naturalHeight:450});
    check("Loaded thumbnails supply missing widescreen dimensions", portfolioTileSize(unspecifiedPhoto).width === 4 &&
      portfolioTileSize(unspecifiedPhoto).height === 2);
    photoTimeline.sourceRatios.delete(-1); photoTimeline.sourceRatios.delete(-2);
    const normalSize = portfolioLayout.base;
    const sharpCentre = {sharp_center_l:"very sharp", sharp_edge_l:"soft"};
    const sizes = [
      [{mark:"great", width:4000, height:6000}, 3, 4],
      [{mark:"5", width:6000, height:4000}, 4, 3],
      [{rating:5, width:1600, height:900}, 4, 2],
      [{mark:"5", width:2000, height:900}, 4, 2],
      [{mark:"great", width:1600, height:900, ...sharpCentre}, 4, 4],
      [{mark:"5", width:900, height:1600, ...sharpCentre, sharp_edge_l:"very soft"}, 4, 4],
      [{mark:"5", width:1600, height:900, ...sharpCentre, sharp_edge_l:null}, 4, 2],
      [{mark:"great", width:4000, height:6000, sharp_center_l:null, sharp_edge_l:"soft"}, 3, 4],
      [{mark:"5", width:1600, height:900, ...sharpCentre, sharp_edge_l:"medium"}, 4, 2],
      [{mark:"5", width:800, height:800, ...sharpCentre, sharp_center_l:"sharp"}, 4, 3],
      [{mark:"4", width:1600, height:900, ...sharpCentre}, 1, 1],
      [{width:6000, height:4000, ...sharpCentre}, 1, 1]
    ];
    check("Large grid sizes follow rating, aspect ratio and complete sharpness bands", [109, 360, 1280, 2560, 3840].every(width => {
      const layout = portfolioMosaic(sizes.map(([item]) => item), width);
      return sizes.every(([, columns, rows], i) => tileSpans(layout, layout.boxes[i], columns, rows) &&
        alignedToGrid(layout, layout.boxes[i]) && layout.boxes[i].left + layout.boxes[i].width <= width + .01);
    }));
    check("Ordinary tiles pack against landscape and portrait heroes through their standard gutter", [360, 1280, 2560, 3840].every(width => {
      return [[6000, 4000], [4000, 6000]].every(([imageWidth, imageHeight]) => {
        const items = [{mark:"great", width:imageWidth, height:imageHeight}, ...Array.from({length:149}, () => ({mark:"4"}))];
        const layout = portfolioMosaic(items, width), [hero, first] = layout.boxes;
        if (hero.width + layout.gap + first.width > width + .01)
          return first.top >= hero.top + hero.height + layout.gap - .01;
        const beside = layout.boxes.filter(box => box.left >= hero.left + hero.width + layout.gap - .01 &&
          box.top < hero.top + hero.height && tileNeighbors(hero, box, layout.gap));
        return new Set(beside.map(box => box.top)).size > 1 && first.top === hero.top &&
          Math.abs(first.left - hero.width - layout.gap) < .01 &&
          connectedTiles(layout.boxes, layout.gap);
      });
    }));
    check("Date tiles use ordinary slots and consecutive days share rows", [360, 1280, 2560, 3840].every(width => {
      const a = {}, b = {}, entries = [
        {portfolioDate:"2020-01-01", portfolioDay:a}, {portfolioDay:a},
        {portfolioDate:"2020-01-02", portfolioDay:b}, {portfolioDay:b}, {portfolioDay:b}];
      const layout = portfolioMosaic(entries, width), first = layout.boxes[0], second = layout.boxes[2];
      return Math.abs(first.width - layout.base) < .01 && Math.abs(first.height - layout.base) < .01 &&
        Math.abs(second.width - layout.base) < .01 && Math.abs(first.top - second.top) < .01 &&
        Math.abs(second.left - 2 * (layout.base + 6)) < .01;
    }));
    const columnsAt = width => {
      const {base, gap} = portfolioMosaic([], width);
      return Math.floor((width + gap + .001) / (base + gap));
    };
    check("A nearly tile-wide remainder becomes a seventeenth photo column", (() => {
      const width = 2639, photos = Array.from({length:18}, () => ({}));
      const layout = portfolioMosaic(photos, width), last = layout.boxes[16], next = layout.boxes[17];
      return last.top === 0 && Math.abs(last.left + last.width - width) < .01 &&
        next.left === 0 && Math.abs(next.top - layout.base - layout.gap) < .01;
    })());
    check("A date at a tight row edge stays connected to its first photo", [360, 1280, 3840].every(width => {
      const a = {}, b = {}, columns = columnsAt(width);
      const entries = [{portfolioDate:"2020-01-01", portfolioDay:a},
        ...Array.from({length:columns - 2}, () => ({portfolioDay:a})),
        {portfolioDate:"2020-01-02", portfolioDay:b}, {portfolioDay:b}];
      const layout = portfolioMosaic(entries, width), date = layout.boxes.at(-2), photo = layout.boxes.at(-1);
      return tileNeighbors(date, photo, layout.gap) && mosaicHasConnectedDays(layout, entries);
    }));
    check("October 13 fills the last partial row after a long October 15", [8, 24].every(fullRows => {
      const width = 3840, columns = columnsAt(width);
      const counts = {"15":fullRows * columns + 4, "13":9, "11":10, "09":5, "01":79};
      const days = ["15", "13", "11", "09", "01"].map(date => {
        const day = {day:"2023-10-" + date};
        return [{portfolioDate:day.day, portfolioDay:day},
          ...Array.from({length:counts[date]}, () => ({portfolioDay:day}))];
      });
      const entries = days.flat(), layout = portfolioMosaic(entries, width);
      const start = days[0].length, tail = layout.boxes[start - 1];
      const october13 = layout.boxes.slice(start, start + days[1].length);
      return Math.abs(october13[0].left - tail.left - tail.width - layout.gap) < .01 &&
        october13.every(box => Math.abs(box.top - tail.top) < .01) && mosaicHasConnectedDays(layout, entries);
    }));
    check("A long ordinary day starting on the right uses the whole next row", (() => {
      const width = 3840, columns = columnsAt(width);
      const entries = [79, 5, 10, 9, 132].flatMap((count, i) => {
        const day = {day:"2023-10-" + ["01", "09", "11", "13", "15"][i]};
        return [{portfolioDate:day.day, portfolioDay:day}, ...Array.from({length:count}, () => ({portfolioDay:day}))];
      });
      const layout = portfolioMosaic(entries, width), pitch = layout.base + layout.gap;
      const positions = layout.boxes.map(b => Math.round(b.top / pitch) * columns + Math.round(b.left / pitch));
      return columns === 16 && positions.length === 240 && new Set(positions).size === 240 &&
        Math.round((layout.height + layout.gap) / pitch) === 15 && mosaicHasConnectedDays(layout, entries);
    })());
    const portfolioShape = lines => ({bottom:lines.length, boxes:lines.flatMap((line, top) =>
      [...line].flatMap((cell, left) => cell === "#" ? [{left, top, width:1, height:1}] : []))});
    check("A date beside a 4x3 photo fits at either edge with occupied cells under the date", (() => {
      const group = {date:true, sizes:[portfolioTileSizes.ordinary, portfolioTileSizes.landscape]};
      const prior = portfolioShape(["#.........", "##........", "##........"]);
      const options = portfolioDayOptions(group, prior.boxes, prior.boxes[0], prior.bottom, 10, true, true);
      return [1, 5].every(x => options.some(({boxes:[date, photo]}) =>
        date.left === x && date.top === 0 && photo.left === x + 1 && photo.top === 0));
    })());
    check("A long ordinary day can fill both sides of the July portrait and join below it", (() => {
      const cell = portfolioTileSizes.ordinary, hero = portfolioTileSizes.landscape;
      const groups = [Array(25).fill(cell), [cell, hero, cell, cell], Array(69).fill(cell)]
        .map(sizes => ({date:true, sizes}));
      const layout = portfolioPacking(groups, 17);
      return layout.bottom === 7 && layout.gaps <= 4;
    })());
    check("A date fits a one-cell opening above its first large photo", (() => {
      const group = {date:true, sizes:[portfolioTileSizes.ordinary, portfolioTileSizes.landscape]};
      const prior = portfolioShape(["######", "#.####", "#.....", "#.....", "#....."]);
      const options = portfolioDayOptions(group, prior.boxes, prior.boxes[0], prior.bottom, 6, true, true);
      return options.some(({boxes:[date, photo]}) =>
        date.left === 1 && date.top === 1 && photo.left === 1 && photo.top === 2) &&
        options.every(({boxes}) => boxes.every(b => prior.boxes.every(p =>
          b.left + b.width <= p.left || p.left + p.width <= b.left ||
          b.top + b.height <= p.top || p.top + p.height <= b.top)));
    })());
    check("Portfolio keeps the shorter complete layout for mixed days", (() => {
      // The first fixture improves with exact footprints. The second is better with the original placements.
      return [
        {days:[[9, 16, 9], [2, 4, 3]], rows:3},
        {days:[[12, 4, 3], [4, 3, 4], [2, 4, 3]], rows:4}
      ].every(({days, rows}) => {
        const entries = days.flatMap(([count, width, height], i) => {
          const day = {day:"2023-05-0" + (i + 1)};
          return [{portfolioDate:day.day, portfolioDay:day}, {portfolioDay:day, mark:"5", width, height},
            ...Array.from({length:count - 1}, () => ({portfolioDay:day}))];
        });
        const layout = portfolioMosaic(entries, 3840);
        return Math.round((layout.height + layout.gap) / (layout.base + layout.gap)) === rows &&
          mosaicHasConnectedDays(layout, entries);
      });
    })());
    check("Equal-height Portfolio candidates favor empty cells at the end, then smaller interior gaps", (() => {
      const joined = portfolioShape(["##..##", "##..##", "######"]);
      const split = portfolioShape(["##.##.", "##.##.", "######"]);
      const atEnd = portfolioShape(["######", "######", "##...."]);
      const prefix = {top:0, edge:Array(6).fill(null), largest:0};
      const choose = options => choosePortfolioPlacement(options, [], 0, 6, null, prefix);
      return choose([joined, split]) === split && choose([split, atEnd]) === atEnd;
    })());
    check("Hide dates can use a corner connection when it leaves smaller interior gaps", (() => {
      const {ordinary, portrait} = portfolioTileSizes;
      const groups = [[portrait, ordinary], [ordinary], Array(6).fill(ordinary), [ordinary]]
        .map(sizes => ({date:false, sizes}));
      const baseline = portfolioPacking(groups, 16), packed = portfolioPacking(groups, 16, true);
      return portfolioCornerDays(baseline, groups, 16) === 0 && portfolioCornerDays(packed, groups, 16) > 0 &&
        packed.bottom === baseline.bottom && comparePortfolioLayouts(packed, baseline, 16) < 0;
    })());
    check("Repacking adjacent days in Hide dates can remove a row and fill interior gaps", (() => {
      const {ordinary, portrait, wide, square} = portfolioTileSizes;
      const groups = [[wide, portrait, 3], [wide, 13], [wide, wide, 7], [square, square, 5]]
        .map(description => ({date:false, sizes:[...description.slice(0, -1), ...Array(description.at(-1)).fill(ordinary)]}));
      const baseline = portfolioPacking(groups, 16), packed = portfolioPacking(groups, 16, true);
      const cornersOnly = [false, true].map(exact => packPortfolioDays(groups, 16, exact, {corners:true}));
      return baseline.bottom === 8 && packed.bottom === 7 && packed.gaps === 3 &&
        cornersOnly.every(candidate => comparePortfolioLayouts(packed, candidate, 16) < 0);
    })());
    check("A gap crossing a fixed row boundary is measured independently for each candidate", (() => {
      const {boxes} = portfolioShape(["#.##.#", "#.##.#", "#....#", "##..##"]);
      const prefix = portfolioGapFrontier(boxes, 6, {top:0, edge:Array(6).fill(null), largest:0}, 2);
      const joined = portfolioGapFrontier(boxes, 6, prefix, 4);
      const other = portfolioShape(["######", "######", "#.##.#", "######"]);
      return joined.largest === 10 && portfolioGapFrontier(other.boxes, 6, prefix, 4).largest === 3;
    })());
    check("July 29 shifts its last row right to let July 30 use the open cells", [0, 4].every(fullRows => {
      const width = 3840, columns = columnsAt(width);
      const dates = ["07-27", "07-29", "07-30", "08-20", "08-21"];
      const counts = [fullRows * columns + 5, 15, 15, 15, 10];
      const days = dates.map((date, i) => {
        const day = {day:"2023-" + date};
        return [{portfolioDate:day.day, portfolioDay:day},
          ...Array.from({length:counts[i]}, (_, j) => i === 4 && !j
            ? {portfolioDay:day, mark:"5", width:1600, height:900} : {portfolioDay:day})];
      });
      const entries = days.flat(), layout = portfolioMosaic(entries, width), pitch = layout.base + layout.gap;
      const july29 = layout.boxes.slice(days[0].length, days[0].length + days[1].length);
      const july30 = layout.boxes.slice(days[0].length + days[1].length, days[0].length + days[1].length + days[2].length);
      const sharedTop = july29.at(-1).top;
      const tail = july29.filter(box => box.top === sharedTop), start = july30.filter(box => box.top === sharedTop);
      return tail.length === 6 && start.length === columns - tail.length &&
        Math.abs(tail[0].left - start.length * pitch) < .01 && Math.abs(start[0].left) < .01 &&
        Math.abs(layout.height + layout.gap - (fullRows + 5) * pitch) < .01 &&
        mosaicHasConnectedDays(layout, entries);
    }));
    check("Days with 15 photos fill space beside each other's large photos", [2639, 3840].every(width => {
      const days = Array.from({length:5}, (_, i) => {
        const day = {day:"2023-05-0" + (i + 4)};
        return [{portfolioDate:day.day, portfolioDay:day}, {portfolioDay:day, mark:"5", width:i % 3 ? 4500 : 4000, height:3000},
          ...Array.from({length:14}, () => ({portfolioDay:day}))];
      });
      const entries = days.flat(), layout = portfolioMosaic(entries, width);
      const separateHeight = days.reduce((sum, day) => sum + portfolioMosaic(day, width).height, 0);
      const nextStart = layout.boxes[days[0].length];
      return layout.height < .75 * separateHeight && nextStart.top < layout.boxes[1].top + layout.boxes[1].height &&
        mosaicHasConnectedDays(layout, entries);
    }));
    check("Short days around a large portrait stay connected in either date order", ["asc", "desc"].every(order => {
      const dates = ["11-01", "11-04", "11-08", "11-14", "11-18", "11-19", "11-28", "12-07", "12-09", "12-16", "12-24"];
      const days = [13, 3, 2, 14, 4, 14, 1, 2, 1, 10, 15].map((count, i) => ({day:"2009-" + dates[i], count, large:i === 3}));
      if (order === "desc") days.reverse();
      const entries = days.flatMap(day => [{portfolioDate:day.day, portfolioDay:day},
        ...Array.from({length:day.count}, (_, i) => day.large && !i
          ? {portfolioDay:day, mark:"great", width:3000, height:4000} : {portfolioDay:day})]);
      return [360, 1280, 2639, 3840].every(width => mosaicHasConnectedDays(portfolioMosaic(entries, width), entries));
    }));
    check("Every loaded day has one square date marker", photoTimeline.days.every(day => {
      const date = photoTimeline.portfolioStream.dayStarts.get(day.day);
      return date && Math.abs(date.width - normalSize) < .01 && Math.abs(date.height - normalSize) < .01;
    }));
    check("Portfolio virtualizes a large day's photo nodes", row.photoNodes.size < portfolioDay.total / 2);
    const featured = portfolioDay.allItems[0];
    await openTimelinePhoto(portfolioDay, featured.id);
    check("Portfolio card navigation follows its three-group order", inPortfolioOrder(state.deck));
    // Thumbnail proportions can relayout the mosaic independently of the comment.
    await revealDay(portfolioDay);
    await wait(() => !thumbBusy && !thumbQ.length && !photoTimeline.frame && !photoTimeline.layoutFrame,
      "Portfolio mosaic settled before saving the comment");
    const mosaicHeight = $("timelineStage").style.height;
    const note = ('Portfolio <b> & "text" ' + "A long caption wraps over the image. ".repeat(5)).trim();
    saveComment(state.shown, note);
    await wait(() => featured.comment === note, "Portfolio comment saved");
    await settled(); closeDetail(); row = await revealDay(portfolioDay);
    const featuredTile = row.portfolioNodes.get(0), caption = featuredTile.querySelector(".portfolio-caption");
    const imageRect = featuredTile.firstElementChild.getBoundingClientRect(), captionRect = caption.getBoundingClientRect();
    check("Featured comments overlay the image bottom as plain text without extending the mosaic", caption.textContent === note &&
      !caption.querySelector("b") && captionRect.height > 0 && captionRect.top >= imageRect.top - 1 &&
      Math.abs(captionRect.bottom - imageRect.bottom) < 1 && caption.scrollHeight <= caption.clientHeight + 1 &&
      Math.abs(featuredTile.getBoundingClientRect().height - imageRect.height) < 1 && $("timelineStage").style.height === mosaicHeight);
    scrollTo(0, document.documentElement.scrollHeight); schedulePhotoTimeline();
    await wait(() => !featuredTile.isConnected, "featured tile scrolled out of range");
    await revealDay(portfolioDay);
    await wait(() => photoTimeline.mounted.get(portfolioDay.day)?.portfolioNodes.get(0)?.querySelector(".portfolio-caption").textContent === note,
      "saved caption on remounted featured tile");
    const ordinaryPhoto = portfolioDay.allItems.find(it => !portfolioFeatured(it));
    await openTimelinePhoto(portfolioDay, ordinaryPhoto.id); saveComment(state.shown, "Hidden ordinary comment");
    await wait(() => ordinaryPhoto.comment === "Hidden ordinary comment", "ordinary comment saved"); closeDetail();
    const ordinaryIndex = portfolioDay.allItems.indexOf(ordinaryPhoto);
    const stageTop = $("timelineStage").getBoundingClientRect().top + scrollY;
    scrollTo(0, stageTop + portfolioDay.portfolioLayout.boxes[ordinaryIndex].top);
    schedulePhotoTimeline();
    await wait(() => photoTimeline.mounted.get(portfolioDay.day)?.portfolioNodes.has(ordinaryIndex), "ordinary Portfolio tile mounted");
    row = photoTimeline.mounted.get(portfolioDay.day);
    check("Other photos never display comment captions", row.portfolioNodes.get(ordinaryIndex).querySelector(".portfolio-caption").hidden);
    await openTimelinePhoto(portfolioDay, ordinaryPhoto.id);
    $("detail").querySelector(".rate .great").click();
    await wait(() => state.shown.mark === "great" && !photoTimeline.updating.size, "Portfolio promotion"); await settled();
    check("Rating edits update Portfolio size, grouping and navigation", inPortfolioOrder(portfolioDay.allItems) &&
      portfolioFeatured(portfolioDay.allItems.find(it => it.id === ordinaryPhoto.id)) && inPortfolioOrder(state.deck));
    closeDetail();

    change("timelineLimit", true); await settled();
    const portfolioPreview = photoTimeline.days.find(day => day.day === "2020-01-03");
    row = await revealDay(portfolioPreview); await settled();
    check("Portfolio shows exactly fifteen photos plus a separate date tile", portfolioPreview.items.length === 15 &&
      portfolioPreview.portfolioLayout.boxes.length === 15 &&
      photoTimeline.portfolioStream.entries.filter(it => it.portfolioDay === portfolioPreview).length === 16 &&
      $("timelineLimitLabel").textContent === "15 per day");
    const originalFifteen = portfolioPreview.candidates.slice(0, 15).map(it => it.id);
    check("The exact fifteen retain highlight priority and client order", originalFifteen.every(id => portfolioPreview.items.some(it => it.id === id)) &&
      inPortfolioOrder(portfolioPreview.items));
    await openTimelinePhoto(portfolioPreview, portfolioPreview.items[0].id);
    const previewStage = $("timelineStage"), previousWidth = previewStage.clientWidth;
    const outlineColor = photoTimeline.portfolioStream.regions.get(portfolioPreview).color;
    previewStage.style.width = Math.floor(previousWidth * .7) + "px"; layoutPhotoTimeline(); await settled();
    check("The resized Portfolio still reaches the right edge", rightEdgeFilled());
    check("Resizing recalculates the shared mosaic and keeps exactly fifteen", photoTimeline.portfolioStream.width < previousWidth &&
      portfolioPreview.items.length === 15 && photoTimeline.portfolioStream.rows.every(line => {
        return line.indices.every(i => {
          const box = photoTimeline.portfolioStream.boxes[i];
          return box.left + box.width <= previewStage.clientWidth + .01;
        });
      }) && originalFifteen.every(id => portfolioPreview.items.some(it => it.id === id)) &&
      photoTimeline.portfolioStream.regions.get(portfolioPreview).color === outlineColor);
    check("Resizing also updates the open photo's navigation selection", state.deck === portfolioPreview.items && inPortfolioOrder(state.deck));
    previewStage.style.width = ""; layoutPhotoTimeline(); await settled();
    closeDetail();
    const smallDay = photoTimeline.days.find(day => day.total < 15);
    check("Short days show every available photo", smallDay && smallDay.items.length === smallDay.total);
    check("Portfolio contains no Show all, Analyse day or tag buttons", !$("timelineStage").querySelector(
      ".timeline-toggle-all,.timeline-add-tags,.timeline-remove-tags,.timeline-date,.timeline-actions"));
    change("timelineLimit", false); await settled();
    const unlimitedPortfolio = photoTimeline.days.find(day => day.day === "2020-01-03");
    await revealDay(unlimitedPortfolio); await settled();
    check("Disabling the limit automatically loads every Portfolio photo", unlimitedPortfolio.allNext === null &&
      unlimitedPortfolio.allItems.length === unlimitedPortfolio.total && inPortfolioOrder(unlimitedPortfolio.allItems));
    change("timelineRating", "good"); change("timelineUnrated", false); await settled();
    for (const day of photoTimeline.days){
      await revealDay(day);
      await wait(() => day.allNext === null, "full rated Portfolio day");
    }
    await settled();
    const ratedStream = photoTimeline.portfolioStream;
    check("Good only without unrated keeps days and their date tiles in visual order", photoTimeline.days.length > 1 &&
      ratedStream.entries.every(it => it.portfolioDate || ["great", "5", "4"].includes(effRating(it))) &&
      mosaicHasConnectedDays(ratedStream, ratedStream.entries) && photoTimeline.days.every((day, i, days) =>
        inPortfolioOrder(day.allItems) && (!i || day.day < days[i - 1].day)));
    change("timelineLimit", true); await settled();
    const restoredPreview = photoTimeline.days.find(day => day.day === "2020-01-03");
    change("timelineView", "timeline"); await settled();
    row = await revealDay(restoredPreview);
    check("Switching back restores Timeline sorting, badges, day controls and exact limit", restoredPreview.items.length === 15 &&
      inCaptureOrder(restoredPreview.items) && row.querySelector(".timeline-badge") && row.querySelector(".timeline-toggle-all") &&
      row.querySelector(".timeline-add-tags") && !row.querySelector(".portfolio-tile") && !$("portfolioFillers") && !$("portfolioOutlines"));
    row.querySelector(".timeline-toggle-all").click(); await settled();
    change("timelineView", "portfolio"); await settled();
    check("Portfolio's limit overrides a manually expanded Timeline day", !restoredPreview.expanded &&
      timelineItems(restoredPreview).length === 15 && !$("timelineStage").querySelector(".timeline-toggle-all"));
    change("timelineView", "timeline"); await settled();
    check("Returning to Timeline restores its previous expansion", restoredPreview.expanded);
    check("Portfolio introduced no horizontal overflow", document.documentElement.scrollWidth <= innerWidth + 1);

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
            con.execute("UPDATE photos SET color_hex=CASE WHEN id%2=0 THEN '#68765b' ELSE '#836147' END WHERE id <= 1030")
            con.execute("UPDATE photos SET width=CASE WHEN id%3=0 THEN 800 ELSE 1200 END, "
                        "height=CASE WHEN id%3=1 THEN 1800 ELSE 800 END")
            con.execute("UPDATE photos SET sharp_center=55, sharp_edge=12 WHERE id IN (25, 26)")
            con.executemany("INSERT INTO photos(id,path,filename,ext,size,taken,year,month,ym,mark,camera,lens) "
                            "VALUES (?,?,?,'jpg',1000,?,2019,1,'2019-01',?,'Camera 00','Lens 00')",
                            [(3001+i, f"bulk/{i}.jpg", f"bulk-{i}.jpg", f"2019-01-01 12:00:{i:02d}",
                              "great" if i < 24 else "bad" if i < 32 else None) for i in range(40)])
            con.executemany("INSERT INTO photo_tags VALUES (?,'base')", [(3001+i,) for i in range(40)])
            con.executemany("UPDATE photos SET pair_stem=?, date_src='exif' WHERE id=?",
                            [(raw_pairs.stem(filename), pid) for pid, filename in
                             con.execute("SELECT id, filename FROM photos").fetchall()])
            con.execute("INSERT INTO photos(path,filename,ext,pair_stem,taken,date_src,year,month,ym,mark,camera) "
                        "SELECT path || '.nef', pair_stem || '.NEF', 'nef', pair_stem, taken, 'exif', "
                        "year, month, ym, 'bad', 'Hidden RAW camera' FROM photos WHERE ext='jpg' AND id%3=0")
        pictures = []
        for size in ((80, 80), (80, 120), (120, 80)):
            picture = io.BytesIO()
            Image.new("RGB", size, (104, 118, 91)).save(picture, "JPEG")
            pictures.append(picture.getvalue())

        @app.app.before_request
        def thumbnail():
            if request.path.startswith("/api/thumb/"):
                pid = int(request.path.rsplit("/", 1)[1])
                return Response(pictures[pid % 3], mimetype="image/jpeg")

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
