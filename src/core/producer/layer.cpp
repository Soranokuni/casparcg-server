/*
 * Copyright (c) 2011 Sveriges Television AB <info@casparcg.com>
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
 *
 * Author: Robert Nagy, ronag89@gmail.com
 */

#include "../StdAfx.h"

#include "layer.h"

#include "frame_producer.h"

#include "../frame/draw_frame.h"
#include "../video_format.h"

#include <common/log.h>

#include <deque>
#include <limits>

namespace caspar { namespace core {

namespace {

struct queued_item
{
    spl::shared_ptr<frame_producer> producer  = frame_producer::empty();
    bool                            auto_play = false;
    item_info                       info;
};

void merge_actions(item_info& info, const std::shared_ptr<item_actions>& actions)
{
    if (!info.actions) {
        info.actions = std::make_shared<item_actions>();
    }
    auto& dst = *info.actions;
    dst.stage_ops.insert(dst.stage_ops.end(), actions->stage_ops.begin(), actions->stage_ops.end());
    dst.deferred.insert(dst.deferred.end(), actions->deferred.begin(), actions->deferred.end());
}

} // namespace

struct layer::impl
{
    monitor::state                state_;
    const core::video_format_desc format_desc_;

    spl::shared_ptr<frame_producer> foreground_ = frame_producer::empty();
    spl::shared_ptr<frame_producer> background_ = frame_producer::empty();

    bool auto_play_ = false;
    bool paused_    = false;

    // PlayOut fork
    item_info                                  foreground_info_;
    item_info                                  background_info_;
    std::deque<queued_item>                    queue_;
    bool                                       ended_reported_  = false;
    std::int64_t                               frames_left_     = 0;
    std::vector<layer_event>                   events_;
    std::vector<std::shared_ptr<item_actions>> pending_actions_;

    // A new foreground goes on air on the tick that shows its first frame. Until it is ready (a take
    // waits for its decoder), the transition keeps showing the old item, which stays on air.
    struct pending_switch
    {
        bool                          active = false;
        std::wstring                  visible_tag; // the item still on air
        std::shared_ptr<item_actions> actions;     // of the new foreground
        const char*                   reason = "";
    } switch_;

  public:
    impl(const core::video_format_desc format_desc)
        : format_desc_(format_desc)
    {
    }

    void emit(const char* kind, const std::wstring& tag, const char* reason)
    {
        if (tag.empty())
            return;

        layer_event event;
        event.kind   = kind;
        event.tag    = tag;
        event.reason = reason;
        events_.push_back(std::move(event));
    }

    // Every change of the foreground goes through here, so no event or on-air action is missed.
    // The events and actions follow in report_switch(), when the new foreground is ready.
    void set_foreground(spl::shared_ptr<frame_producer> producer, item_info info, const char* reason)
    {
        // An item replaced before it showed a frame never went on air: it gets no events and its
        // actions do not run.
        if (!switch_.active) {
            switch_.visible_tag = foreground_info_.tag;
        }

        foreground_      = std::move(producer);
        foreground_info_ = std::move(info);
        ended_reported_  = false;

        switch_.active  = true;
        switch_.actions = std::move(foreground_info_.actions);
        switch_.reason  = reason;

        foreground_info_.actions = nullptr;
    }

    // The tag of the item the viewer sees.
    const std::wstring& visible_tag() const { return switch_.active ? switch_.visible_tag : foreground_info_.tag; }

    void report_switch()
    {
        if (!switch_.active || !foreground_->is_ready())
            return;

        emit("offair", switch_.visible_tag, switch_.reason);
        emit("onair", foreground_info_.tag, switch_.reason);

        if (switch_.actions) {
            pending_actions_.push_back(std::move(switch_.actions));
        }
        switch_ = {};
    }

    // The background is the head of the queue.
    void promote_queue()
    {
        if (background_ != frame_producer::empty() || queue_.empty())
            return;

        auto item = std::move(queue_.front());
        queue_.pop_front();

        background_      = std::move(item.producer);
        background_info_ = std::move(item.info);
        auto_play_       = item.auto_play;
    }

    // An AUTO background replaces an empty foreground at once.
    void play_if_auto_ready()
    {
        if (auto_play_ && background_ != frame_producer::empty() && foreground_ == frame_producer::empty()) {
            play("auto");
        }
    }

    void pause() { paused_ = true; }

    void resume() { paused_ = false; }

    void load(spl::shared_ptr<frame_producer> producer,
              bool                            preview_producer,
              bool                            auto_play,
              item_info                       info,
              bool                            clear_queue)
    {
        if (clear_queue) {
            queue_.clear();
        }

        background_      = std::move(producer);
        background_info_ = std::move(info);
        auto_play_       = auto_play;

        if (auto_play_ && foreground_ == frame_producer::empty()) {
            play("auto");
        } else if (preview_producer) {
            preview(true);
        }
    }

    void preview(bool force)
    {
        if (force || background_ != frame_producer::empty()) {
            play("take");
            paused_ = true;
        }
    }

    void play(const char* reason)
    {
        if (background_ != frame_producer::empty()) {
            if (!paused_) {
                background_->leading_producer(foreground_);
            } else {
                if (format_desc_.field_count == 2) {
                    auto frame1 = foreground_->last_frame(core::video_field::a);
                    auto frame2 = foreground_->last_frame(core::video_field::b);
                    background_->leading_producer(
                        spl::make_shared<core::const_producer>(std::move(frame1), std::move(frame2)));
                } else {
                    auto frame = foreground_->last_frame(core::video_field::progressive);
                    background_->leading_producer(spl::make_shared<core::const_producer>(frame, frame));
                }
            }

            auto producer    = background_;
            auto info        = std::move(background_info_);
            background_      = frame_producer::empty();
            background_info_ = {};
            auto_play_       = false;

            set_foreground(std::move(producer), std::move(info), reason);
            promote_queue();
        }

        paused_ = false;
    }

    void stop(const char* reason)
    {
        emit("offair", visible_tag(), reason);
        switch_ = {};

        foreground_      = frame_producer::empty();
        foreground_info_ = {};
        auto_play_       = false;
        ended_reported_  = false;
    }

    std::vector<std::shared_ptr<item_actions>> prepare()
    {
        try {
            // A finished transition hands over to its destination (was at the top of receive()).
            if (foreground_->following_producer() != core::frame_producer::empty()) {
                foreground_ = foreground_->following_producer();
            }

            const auto nb_frames = foreground_->nb_frames();
            const bool ended     = nb_frames != std::numeric_limits<std::uint32_t>::max() &&
                               static_cast<std::int64_t>(foreground_->frame_number()) + 1 >=
                                   static_cast<std::int64_t>(nb_frames);
            if (ended && !ended_reported_) {
                ended_reported_ = true;
                emit("ended", foreground_info_.tag, "ended");
            }

            frames_left_ = 0;
            if (auto_play_) {
                auto auto_play_delta = background_->auto_play_delta();
                if (auto_play_delta) {
                    auto time     = static_cast<std::int64_t>(foreground_->frame_number());
                    auto duration = static_cast<std::int64_t>(nb_frames);
                    frames_left_  = duration - time - *auto_play_delta;
                    if (frames_left_ < 1) {
                        play("auto");
                    }
                }
            }

            report_switch();
        } catch (...) {
            CASPAR_LOG_CURRENT_EXCEPTION();
            stop("error");
        }

        auto actions = std::move(pending_actions_);
        pending_actions_.clear();
        return actions;
    }

    void update_state()
    {
        state_                           = {};
        state_["foreground"]             = foreground_->state();
        state_["foreground"]["producer"] = foreground_->name();
        state_["foreground"]["paused"]   = paused_;

        if (frames_left_ > 0) {
            state_["foreground"]["frames_left"] = frames_left_;
        }

        state_["background"]             = background_->state();
        state_["background"]["producer"] = background_->name();

        // PlayOut fork
        if (!foreground_info_.tag.empty()) {
            state_["foreground"]["tag"] = foreground_info_.tag;
        }
        if (!background_info_.tag.empty()) {
            state_["background"]["tag"]  = background_info_.tag;
            state_["background"]["auto"] = auto_play_;
        }
        if (!queue_.empty()) {
            std::vector<std::wstring> tags;
            for (const auto& item : queue_) {
                tags.push_back(item.info.tag);
            }
            state_["queue"]["tags"] = tags;
        }
    }

    draw_frame receive(const video_field field, int nb_samples)
    {
        try {
            auto frame = paused_ ? core::draw_frame{} : foreground_->receive(field, nb_samples);
            if (!frame) {
                frame = foreground_->last_frame(field);
            }

            update_state();

            return frame;
        } catch (...) {
            CASPAR_LOG_CURRENT_EXCEPTION();
            stop("error");
            return draw_frame{};
        }
    }

    draw_frame receive_background(const video_field field, int nb_samples)
    {
        try {
            return background_->first_frame(field);

        } catch (...) {
            CASPAR_LOG_CURRENT_EXCEPTION();
            background_ = frame_producer::empty();
            return draw_frame{};
        }
    }

    void queue_push(spl::shared_ptr<frame_producer> producer, bool auto_play, item_info info)
    {
        if (background_ == frame_producer::empty()) {
            background_      = std::move(producer);
            background_info_ = std::move(info);
            auto_play_       = auto_play;
            play_if_auto_ready();
        } else {
            queued_item item;
            item.producer  = std::move(producer);
            item.auto_play = auto_play;
            item.info      = std::move(info);
            queue_.push_back(std::move(item));
        }
    }

    int queue_clear(const std::wstring& after_tag)
    {
        if (after_tag.empty() || after_tag == foreground_info_.tag) {
            const int removed = (background_ != frame_producer::empty() ? 1 : 0) + static_cast<int>(queue_.size());
            background_       = frame_producer::empty();
            background_info_  = {};
            auto_play_        = false;
            queue_.clear();
            return removed;
        }

        if (background_ != frame_producer::empty() && background_info_.tag == after_tag) {
            const int removed = static_cast<int>(queue_.size());
            queue_.clear();
            return removed;
        }

        for (auto it = queue_.begin(); it != queue_.end(); ++it) {
            if (it->info.tag == after_tag) {
                const int removed = static_cast<int>(std::distance(std::next(it), queue_.end()));
                queue_.erase(std::next(it), queue_.end());
                return removed;
            }
        }

        return -1;
    }

    bool queue_remove(const std::wstring& tag)
    {
        if (tag.empty())
            return false;

        if (background_ != frame_producer::empty() && background_info_.tag == tag) {
            background_      = frame_producer::empty();
            background_info_ = {};
            auto_play_       = false;
            promote_queue();
            play_if_auto_ready();
            return true;
        }

        for (auto it = queue_.begin(); it != queue_.end(); ++it) {
            if (it->info.tag == tag) {
                queue_.erase(it);
                return true;
            }
        }

        return false;
    }

    attach_result attach_actions(const std::wstring& tag, const std::shared_ptr<item_actions>& actions)
    {
        if (tag.empty())
            return attach_result::not_found;

        if (foreground_info_.tag == tag) {
            // Not on air yet: the actions run with its first frame.
            if (switch_.active) {
                if (!switch_.actions) {
                    switch_.actions = std::make_shared<item_actions>();
                }
                item_info holder{tag, switch_.actions};
                merge_actions(holder, actions);
                return attach_result::attached;
            }
            return attach_result::on_air;
        }

        if (background_ != frame_producer::empty() && background_info_.tag == tag) {
            merge_actions(background_info_, actions);
            return attach_result::attached;
        }

        for (auto& item : queue_) {
            if (item.info.tag == tag) {
                merge_actions(item.info, actions);
                return attach_result::attached;
            }
        }

        return attach_result::not_found;
    }

    layer_snapshot snapshot() const
    {
        layer_snapshot result;
        result.exists              = true;
        result.foreground_tag      = foreground_info_.tag;
        result.foreground_producer = foreground_->name();
        result.frame_number        = foreground_->frame_number();
        result.nb_frames           = foreground_->nb_frames();
        result.paused              = paused_;
        result.starting            = switch_.active;

        if (background_ != frame_producer::empty()) {
            result.waiting.push_back({background_info_.tag, background_->name(), auto_play_});
        }
        for (const auto& item : queue_) {
            result.waiting.push_back({item.info.tag, item.producer->name(), item.auto_play});
        }

        return result;
    }
};

layer::layer(const core::video_format_desc format_desc)
    : impl_(new impl(format_desc))
{
}
layer::layer(layer&& other)
    : impl_(std::move(other.impl_))
{
}
layer& layer::operator=(layer&& other)
{
    impl_ = std::move(other.impl_);
    return *this;
}
void layer::swap(layer& other) { impl_.swap(other.impl_); }
void layer::load(spl::shared_ptr<frame_producer> frame_producer,
                 bool                            preview,
                 bool                            auto_play,
                 item_info                       info,
                 bool                            clear_queue)
{
    return impl_->load(std::move(frame_producer), preview, auto_play, std::move(info), clear_queue);
}
void       layer::play() { impl_->play("take"); }
void       layer::preview() { impl_->preview(false); }
void       layer::pause() { impl_->pause(); }
void       layer::resume() { impl_->resume(); }
void       layer::stop() { impl_->stop("stop"); }
draw_frame layer::receive(const video_field field, int nb_samples) { return impl_->receive(field, nb_samples); }
draw_frame layer::receive_background(const video_field field, int nb_samples)
{
    return impl_->receive_background(field, nb_samples);
}
spl::shared_ptr<frame_producer> layer::foreground() const { return impl_->foreground_; }
spl::shared_ptr<frame_producer> layer::background() const { return impl_->background_; }
bool                            layer::has_background() const { return impl_->background_ != frame_producer::empty(); }
core::monitor::state            layer::state() const { return impl_->state_; }

std::vector<std::shared_ptr<item_actions>> layer::prepare() { return impl_->prepare(); }
const std::wstring&                        layer::foreground_tag() const { return impl_->foreground_info_.tag; }
void layer::queue_push(spl::shared_ptr<frame_producer> producer, bool auto_play, item_info info)
{
    impl_->queue_push(std::move(producer), auto_play, std::move(info));
}
int                  layer::queue_clear(const std::wstring& after_tag) { return impl_->queue_clear(after_tag); }
bool                 layer::queue_remove(const std::wstring& tag) { return impl_->queue_remove(tag); }
layer::attach_result layer::attach_actions(const std::wstring& tag, const std::shared_ptr<item_actions>& actions)
{
    return impl_->attach_actions(tag, actions);
}
void layer::on_clear() { impl_->emit("offair", impl_->visible_tag(), "clear"); }
std::vector<layer_event> layer::take_events()
{
    auto events = std::move(impl_->events_);
    impl_->events_.clear();
    return events;
}
layer_snapshot layer::snapshot() const { return impl_->snapshot(); }
}} // namespace caspar::core
