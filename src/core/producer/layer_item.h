/*
 * Copyright (c) 2026 PlayOutVue contributors
 *
 * This file is part of CasparCG (www.casparcg.com).
 *
 * CasparCG is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * CasparCG is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with CasparCG. If not, see <http://www.gnu.org/licenses/>.
 */

// PlayOut fork: item identity (TAG), on-air actions (ONAIR), the wait queue (QUEUE) and the layer
// event log (EVENTS). See PLAYOUT-FORK.md in the repository root.

#pragma once

#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <string>
#include <vector>

namespace caspar { namespace core {

class stage_sync_ops;

// A stage operation recorded ahead of time. It runs on the stage thread.
using captured_op = std::function<void(stage_sync_ops&)>;

// Work attached to an item that runs when the item goes on air.
struct item_actions
{
    // Applied inside the tick in which the item goes on air, before any layer is drawn, so their
    // result shows on the same frame as the item's first frame.
    std::vector<captured_op> stage_ops;

    // Work that cannot run inside the tick (CG commands). Called once, on the stage thread, right
    // after stage_ops. Must not block: it only hands the work to another thread.
    std::vector<std::function<void()>> deferred;
};

// Identity and on-air actions of an item on a layer.
struct item_info
{
    std::wstring                  tag;
    std::shared_ptr<item_actions> actions;
};

// One entry in a layer's event log.
struct layer_event
{
    std::uint64_t seq     = 0; // per layer index, starts at 1
    std::int64_t  frame   = 0; // channel frame counter of the tick in which the change is visible
    std::int64_t  time_ms = 0; // wall clock of that tick, ms since the Unix epoch
    std::string   kind;        // "onair", "offair", "ended", "stalled", "actionfailed"
    std::wstring  tag;
    std::string   reason; // "auto", "take", "stop", "clear", "error", "stall", "ended", ...
};

// A read-only view of the items on a layer, for QUEUE INFO.
struct layer_snapshot
{
    struct waiting_item
    {
        std::wstring tag;
        std::wstring producer;
        bool         auto_play = false;
    };

    bool                      exists = false;
    std::wstring              foreground_tag;
    std::wstring              foreground_producer;
    std::uint32_t             frame_number = 0;
    std::uint32_t             nb_frames    = std::numeric_limits<std::uint32_t>::max();
    bool                      paused       = false;
    bool                      starting     = false; // the foreground has not shown its first frame yet
    std::vector<waiting_item> waiting;              // the background first, then the queue
};

}} // namespace caspar::core
