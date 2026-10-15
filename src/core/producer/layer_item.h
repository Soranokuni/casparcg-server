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

// PlayOut fork: item identity (TAG) and the layer event log (EVENTS). See PLAYOUT-FORK.md in the
// repository root.

#pragma once

#include <cstdint>
#include <string>

namespace caspar { namespace core {

class stage_sync_ops;

// Identity of an item on a layer.
struct item_info
{
    std::wstring tag;
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

}} // namespace caspar::core
