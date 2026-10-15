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

#include "stage.h"

#include "layer.h"

#include "../frame/draw_frame.h"

#include <common/diagnostics/graph.h>
#include <common/executor.h>
#include <common/future.h>
#include <common/utf.h>

#include <core/frame/frame_transform.h>
#include <core/producer/route/route_producer.h>

#include <boost/range/adaptors.hpp>

#include <chrono>
#include <deque>
#include <functional>
#include <future>
#include <map>
#include <vector>

namespace caspar { namespace core {

namespace {

// PlayOut fork: the event log of a layer index. It outlives the layer (CLEAR).
struct layer_event_log
{
    std::uint64_t           seq = 0;
    std::deque<layer_event> history;
};

const std::size_t max_logged_events = 128;

std::int64_t unix_time_ms()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

} // namespace

struct stage::impl : public std::enable_shared_from_this<impl>
{
    // PlayOut fork: stage_sync_ops for code that already runs on the stage thread.
    struct sync_access final : public stage_sync_ops
    {
        impl& self;

        explicit sync_access(impl& s)
            : self(s)
        {
        }

        layer& get_layer(int index) override { return self.get_layer(index); }
        layer* find_layer(int index) override
        {
            auto it = self.layers_.find(index);
            return it == self.layers_.end() ? nullptr : &it->second;
        }
        std::vector<layer_event> events(int index, std::uint64_t since) const override
        {
            std::vector<layer_event> result;
            auto                     it = self.event_logs_.find(index);
            if (it != self.event_logs_.end()) {
                for (const auto& event : it->second.history) {
                    if (event.seq > since) {
                        result.push_back(event);
                    }
                }
            }
            return result;
        }
        std::uint64_t last_event_seq(int index) const override
        {
            auto it = self.event_logs_.find(index);
            return it == self.event_logs_.end() ? 0 : it->second.seq;
        }
        std::uint64_t frame() const override { return self.last_frame_; }
    };

    int                                 channel_index_;
    spl::shared_ptr<diagnostics::graph> graph_;
    monitor::state                      state_;
    std::map<int, layer>                layers_;
    std::map<int, tweened_transform>    tweens_;
    std::set<int>                       routeSources;

    mutable std::mutex      format_desc_mutex_;
    core::video_format_desc format_desc_;

    // PlayOut fork
    std::map<int, layer_event_log>          event_logs_;
    std::map<int, std::vector<layer_event>> orphan_events_; // events of cleared layers, not yet stamped
    std::uint64_t                           last_frame_ = 0;
    sync_access                             sync_{*this};

    executor   executor_{L"stage " + std::to_wstring(channel_index_)};
    std::mutex lock_;

  private:
    void orderSourceLayers(std::vector<std::pair<int, bool>>&        layerVec,
                           const std::map<int, std::pair<int, int>>& routed_layers,
                           int                                       l,
                           int                                       depth)
    {
        if (0 == depth)
            routeSources.clear();

        if (std::find_if(layerVec.begin(), layerVec.end(), [l](std::pair<int, bool> p) { return p.first == l; }) !=
            layerVec.end()) {
            return;
        }

        auto routeIt = routed_layers.find(l);
        if (routed_layers.end() == routeIt) {
            layerVec.push_back(std::make_pair(l, true));
            return;
        }

        std::pair<int, int> routeSrc(routeIt->second);
        if (channel_index_ != routeSrc.first) {
            layerVec.push_back(std::make_pair(l, true));
            return;
        }

        // check for circular route setup - skip recursion if found
        routeSources.emplace(routeSrc.second);
        bool layerOK = true;
        if (routeSources.find(l) == routeSources.end()) {
            orderSourceLayers(layerVec, routed_layers, routeSrc.second, ++depth);
        } else {
            layerOK = false;
        }

        if (std::find_if(layerVec.begin(), layerVec.end(), [l](std::pair<int, bool> p) { return p.first == l; }) ==
            layerVec.end()) {
            layerVec.push_back(std::make_pair(l, layerOK));
        }
    }

    layer& get_layer(int index)
    {
        auto it = layers_.find(index);
        if (it == std::end(layers_)) {
            it = layers_.emplace(index, layer(video_format_desc())).first;
        }
        return it->second;
    }

    // PlayOut fork: AUTO and the end of the foreground are handled before any layer is drawn.
    void prepare_layers(std::uint64_t frame_number)
    {
        for (auto& p : layers_) {
            p.second.prepare();
        }

        stamp_events(frame_number, unix_time_ms());

        last_frame_ = frame_number;
    }

    void stamp_events(std::uint64_t frame_number, std::int64_t time_ms)
    {
        // Cleared layers first: they went before anything on a new layer at the same index.
        for (auto& p : orphan_events_) {
            for (auto& event : p.second) {
                log_event(p.first, std::move(event), frame_number, time_ms);
            }
        }
        orphan_events_.clear();

        for (auto& p : layers_) {
            for (auto& event : p.second.take_events()) {
                log_event(p.first, std::move(event), frame_number, time_ms);
            }
        }
    }

    void log_event(int index, layer_event event, std::uint64_t frame_number, std::int64_t time_ms)
    {
        auto& log     = event_logs_[index];
        event.seq     = ++log.seq;
        event.frame   = static_cast<std::int64_t>(frame_number);
        event.time_ms = time_ms;

        CASPAR_LOG(info) << L"[event] " << channel_index_ << L"-" << index << L" #" << event.seq << L" "
                         << u16(event.kind) << L" " << event.tag << L" (" << u16(event.reason) << L") frame "
                         << event.frame;

        log.history.push_back(std::move(event));
        while (log.history.size() > max_logged_events) {
            log.history.pop_front();
        }
    }

    // The latest event of each kind stays in the OSC state, so a lost or coalesced packet loses nothing.
    void add_event_state(monitor::state& state, std::uint64_t frame_number) const
    {
        static const char* const kinds[] = {"onair", "offair", "ended", "stalled", "actionfailed"};

        state["frame"] = static_cast<std::int64_t>(frame_number);

        for (const auto& p : event_logs_) {
            const auto& log = p.second;
            if (log.history.empty()) {
                continue;
            }

            state["layer"][p.first]["event"]["seq"] = static_cast<std::int64_t>(log.seq);
            for (const auto* kind : kinds) {
                for (auto it = log.history.rbegin(); it != log.history.rend(); ++it) {
                    if (it->kind == kind) {
                        state["layer"][p.first]["event"][kind] = {
                            static_cast<std::int64_t>(it->seq), it->frame, it->time_ms, it->tag, it->reason};
                        break;
                    }
                }
            }
        }
    }

    void do_clear(int index)
    {
        auto it = layers_.find(index);
        if (it == layers_.end()) {
            return;
        }

        // PlayOut fork: the layer's events go to the log, which outlives the layer.
        it->second.on_clear();
        auto& orphans = orphan_events_[index];
        for (auto& event : it->second.take_events()) {
            orphans.push_back(std::move(event));
        }

        layers_.erase(it);
    }

    void do_clear_all()
    {
        while (!layers_.empty()) {
            do_clear(layers_.begin()->first);
        }
    }

  public:
    impl(int channel_index, spl::shared_ptr<diagnostics::graph> graph, const core::video_format_desc& format_desc)
        : channel_index_(channel_index)
        , graph_(std::move(graph))
        , format_desc_(format_desc)
    {
    }

    const stage_frames operator()(uint64_t                                     frame_number,
                                  std::vector<int>&                            fetch_background,
                                  std::function<void(int, const layer_frame&)> routesCb)
    {
        return executor_.invoke([=] {
            std::map<int, layer_frame> frames;
            stage_frames               result = {};

            result.format_desc = video_format_desc();
            result.nb_samples =
                result.format_desc.audio_cadence[frame_number % result.format_desc.audio_cadence.size()];

            auto is_interlaced = format_desc_.field_count == 2;
            auto field1        = is_interlaced ? video_field::a : video_field::progressive;

            try {
                prepare_layers(frame_number);

                for (auto& t : tweens_)
                    t.second.tick(1);

                // build a map of layers that are sourced from route producers
                std::map<int, std::pair<int, int>> routed_layers;
                for (auto& p : layers_) {
                    auto producer = std::move(p.second.foreground());
                    if (0 == producer->name().compare(L"route")) {
                        try {
                            auto rc       = spl::dynamic_pointer_cast<core::route_control>(producer);
                            auto srcChan  = rc->get_source_channel();
                            auto srcLayer = rc->get_source_layer();
                            routed_layers.emplace(p.first, std::make_pair(srcChan, srcLayer));
                            rc->set_cross_channel(channel_index_ != srcChan);
                        } catch (std::bad_cast) {
                            CASPAR_LOG(error) << "Failed to cast route producer";
                        }
                    }
                }

                // sort layer order so that sources get pulled before routes
                std::vector<std::pair<int, bool>> layerVec;
                for (auto& p : layers_)
                    orderSourceLayers(layerVec, routed_layers, p.first, 0);

                // when running interlaced, both fields are be pulled at once.
                // This will risk some stutter for freshly created producers, but it lets us tick at 25hz and avoids
                // amcp changes starting on the second field

                for (auto& l : layerVec) {
                    auto p = layers_.find(l.first);
                    if (p == layers_.end())
                        continue;

                    auto& layer = p->second;
                    auto& tween = tweens_[p->first];

                    auto has_background_route =
                        std::find(fetch_background.begin(), fetch_background.end(), p->first) != fetch_background.end();

                    layer_frame res = {};
                    if (l.second) {
                        res.foreground1 = draw_frame::push(layer.receive(field1, result.nb_samples), tween.fetch());
                        res.foreground1.transform().image_transform.enable_geometry_modifiers = true;
                    }

                    res.has_background = layer.has_background();
                    if (has_background_route)
                        res.background1 = layer.receive_background(field1, result.nb_samples);

                    if (is_interlaced) {
                        res.is_interlaced = true;
                        if (l.second) {
                            res.foreground2 =
                                draw_frame::push(layer.receive(video_field::b, result.nb_samples), tween.fetch());
                            res.foreground2.transform().image_transform.enable_geometry_modifiers = true;
                        }
                        if (has_background_route)
                            res.background2 = layer.receive_background(video_field::b, result.nb_samples);
                    }

                    frames[p->first] = res;

                    // push received foreground frame to any configured route producer
                    routesCb(p->first, res);
                }

                for (auto& p : frames) {
                    result.frames.push_back(p.second.foreground1);
                    if (is_interlaced)
                        result.frames2.push_back(p.second.foreground2);
                }

                {
                    // push stage_frames to support any channel routes that have been set
                    layer_frame chan_lf   = {};
                    chan_lf.is_interlaced = is_interlaced;
                    chan_lf.foreground1   = wrap_layer_frames_for_route(result.frames);
                    if (is_interlaced)
                        chan_lf.foreground2 = wrap_layer_frames_for_route(result.frames2);

                    routesCb(-1, chan_lf);
                }

                monitor::state state;
                for (auto& p : layers_) {
                    state["layer"][p.first] = p.second.state();
                }
                add_event_state(state, frame_number);
                state_ = std::move(state);
            } catch (...) {
                CASPAR_LOG_CURRENT_EXCEPTION();
                try {
                    do_clear_all();
                } catch (...) {
                    layers_.clear();
                }
            }

            return result;
        });
    }

    core::draw_frame wrap_layer_frames_for_route(std::vector<core::draw_frame> frames)
    {
        // Note: this must not mutate the vector used for the layer
        for (auto& frame : frames) {
            // Tell the compositor that these are layers, matching what normal rendering does
            frame.transform().image_transform.layer_depth = 1;
        }
        return core::draw_frame(frames);
    }

    std::future<void>
    apply_transforms(const std::vector<std::tuple<int, stage::transform_func_t, unsigned int, tweener>>& transforms)
    {
        return executor_.begin_invoke([=] {
            for (auto& transform : transforms) {
                auto& tween = tweens_[std::get<0>(transform)];
                auto  src   = tween.fetch();
                auto  dst   = std::get<1>(transform)(tween.dest());
                tweens_[std::get<0>(transform)] =
                    tweened_transform(src, dst, std::get<2>(transform), std::get<3>(transform));
            }
        });
    }

    std::future<void> apply_transform(int                            index,
                                      const stage::transform_func_t& transform,
                                      unsigned int                   mix_duration,
                                      const tweener&                 tween)
    {
        return executor_.begin_invoke([=] {
            auto src       = tweens_[index].fetch();
            auto dst       = transform(src);
            tweens_[index] = tweened_transform(src, dst, mix_duration, tween);
        });
    }

    std::future<void> clear_transforms(int index)
    {
        return executor_.begin_invoke([=] { tweens_.erase(index); });
    }

    std::future<void> clear_transforms()
    {
        return executor_.begin_invoke([=] { tweens_.clear(); });
    }

    std::future<frame_transform> get_current_transform(int index)
    {
        return executor_.begin_invoke([=] { return tweens_[index].fetch(); });
    }

    std::future<void>
    load(int index, const spl::shared_ptr<frame_producer>& producer, bool preview, bool auto_play, const item_info& info)
    {
        return executor_.begin_invoke([=] { get_layer(index).load(producer, preview, auto_play, info); });
    }

    std::future<void> invoke_sync(std::function<void(stage_sync_ops&)> func)
    {
        return executor_.begin_invoke([=] { func(sync_); });
    }

    std::future<void> preview(int index)
    {
        return executor_.begin_invoke([=] { get_layer(index).preview(); });
    }

    std::future<void> pause(int index)
    {
        return executor_.begin_invoke([=] { get_layer(index).pause(); });
    }

    std::future<void> resume(int index)
    {
        return executor_.begin_invoke([=] { get_layer(index).resume(); });
    }

    std::future<void> play(int index)
    {
        return executor_.begin_invoke([=] { get_layer(index).play(); });
    }

    std::future<void> stop(int index)
    {
        return executor_.begin_invoke([=] { get_layer(index).stop(); });
    }

    std::future<void> clear(int index)
    {
        return executor_.begin_invoke([=] { do_clear(index); });
    }

    std::future<void> clear()
    {
        return executor_.begin_invoke([=] { do_clear_all(); });
    }

    std::future<void> swap_layers(const std::shared_ptr<stage>& other, bool swap_transforms)
    {
        auto other_impl = other->impl_;

        if (other_impl.get() == this) {
            return make_ready_future();
        }

        auto func = [=] {
            auto layers       = layers_ | boost::adaptors::map_values;
            auto other_layers = other_impl->layers_ | boost::adaptors::map_values;

            std::swap(layers_, other_impl->layers_);

            if (swap_transforms)
                std::swap(tweens_, other_impl->tweens_);
        };

        return invoke_both(other, func);
    }

    std::future<void> swap_layer(int index, int other_index, bool swap_transforms)
    {
        return executor_.begin_invoke([=] {
            std::swap(get_layer(index), get_layer(other_index));

            if (swap_transforms)
                std::swap(tweens_[index], tweens_[other_index]);
        });
    }

    std::future<void> swap_layer(int index, int other_index, const std::shared_ptr<stage>& other, bool swap_transforms)
    {
        auto other_impl = other->impl_;

        if (other_impl.get() == this)
            return swap_layer(index, other_index, swap_transforms);
        auto func = [=] {
            auto& my_layer    = get_layer(index);
            auto& other_layer = other_impl->get_layer(other_index);

            std::swap(my_layer, other_layer);

            if (swap_transforms) {
                auto& my_tween    = tweens_[index];
                auto& other_tween = other_impl->tweens_[other_index];
                std::swap(my_tween, other_tween);
            }
        };

        return invoke_both(other, func);
    }

    std::future<void> invoke_both(const std::shared_ptr<stage>& other, std::function<void()> func)
    {
        auto other_impl = other->impl_;

        if (other_impl->channel_index_ < channel_index_) {
            return other_impl->executor_.begin_invoke([=] { executor_.invoke(func); });
        }

        return executor_.begin_invoke([=] { other_impl->executor_.invoke(func); });
    }

    std::future<std::shared_ptr<frame_producer>> foreground(int index)
    {
        return executor_.begin_invoke(
            [=]() -> std::shared_ptr<frame_producer> { return get_layer(index).foreground(); });
    }

    std::future<std::shared_ptr<frame_producer>> background(int index)
    {
        return executor_.begin_invoke(
            [=]() -> std::shared_ptr<frame_producer> { return get_layer(index).background(); });
    }

    std::future<std::wstring> call(int index, const std::vector<std::wstring>& params)
    {
        return flatten(executor_.begin_invoke([=] { return get_layer(index).foreground()->call(params).share(); }));
    }
    std::future<std::wstring> callbg(int index, const std::vector<std::wstring>& params)
    {
        return flatten(executor_.begin_invoke([=] { return get_layer(index).background()->call(params).share(); }));
    }

    std::unique_lock<std::mutex> get_lock() { return std::move(std::unique_lock<std::mutex>(lock_)); }

    core::video_format_desc video_format_desc() const
    {
        std::lock_guard<std::mutex> lock(format_desc_mutex_);
        return format_desc_;
    }

    std::future<void> video_format_desc(const core::video_format_desc& format_desc)
    {
        return executor_.begin_invoke([=] {
            {
                std::lock_guard<std::mutex> lock(format_desc_mutex_);
                format_desc_ = format_desc;
            }

            do_clear_all();
        });
    }
};

stage::stage(int channel_index, spl::shared_ptr<diagnostics::graph> graph, const core::video_format_desc& format_desc)
    : impl_(new impl(channel_index, std::move(graph), format_desc))
{
}
std::future<std::wstring> stage::call(int index, const std::vector<std::wstring>& params)
{
    return impl_->call(index, params);
}
std::future<std::wstring> stage::callbg(int index, const std::vector<std::wstring>& params)
{
    return impl_->callbg(index, params);
}
std::future<void> stage::apply_transforms(const std::vector<stage::transform_tuple_t>& transforms)
{
    return impl_->apply_transforms(transforms);
}
std::future<void> stage::apply_transform(int                                                                index,
                                         const std::function<core::frame_transform(core::frame_transform)>& transform,
                                         unsigned int   mix_duration,
                                         const tweener& tween)
{
    return impl_->apply_transform(index, transform, mix_duration, tween);
}
std::future<void>            stage::clear_transforms(int index) { return impl_->clear_transforms(index); }
std::future<void>            stage::clear_transforms() { return impl_->clear_transforms(); }
std::future<frame_transform> stage::get_current_transform(int index) { return impl_->get_current_transform(index); }
std::future<void>
stage::load(int index, const spl::shared_ptr<frame_producer>& producer, bool preview, bool auto_play, item_info info)
{
    return impl_->load(index, producer, preview, auto_play, info);
}
std::future<void> stage::invoke_sync(std::function<void(stage_sync_ops&)> func)
{
    return impl_->invoke_sync(std::move(func));
}
std::future<void> stage::preview(int index) { return impl_->preview(index); }
std::future<void> stage::pause(int index) { return impl_->pause(index); }
std::future<void> stage::resume(int index) { return impl_->resume(index); }
std::future<void> stage::play(int index) { return impl_->play(index); }
std::future<void> stage::stop(int index) { return impl_->stop(index); }
std::future<void> stage::clear(int index) { return impl_->clear(index); }
std::future<void> stage::clear() { return impl_->clear(); }
std::future<void> stage::swap_layers(const std::shared_ptr<stage_base>& other, bool swap_transforms)
{
    const auto other2 = std::static_pointer_cast<stage>(other);
    return impl_->swap_layers(other2, swap_transforms);
}
std::future<void> stage::swap_layer(int index, int other_index, bool swap_transforms)
{
    return impl_->swap_layer(index, other_index, swap_transforms);
}
std::future<void>
stage::swap_layer(int index, int other_index, const std::shared_ptr<stage_base>& other, bool swap_transforms)
{
    const auto other2 = std::static_pointer_cast<stage>(other);
    return impl_->swap_layer(index, other_index, other2, swap_transforms);
}
std::future<std::shared_ptr<frame_producer>> stage::foreground(int index) { return impl_->foreground(index); }
std::future<std::shared_ptr<frame_producer>> stage::background(int index) { return impl_->background(index); }
const stage_frames                           stage::operator()(uint64_t                                     frame_number,
                                     std::vector<int>&                            fetch_background,
                                     std::function<void(int, const layer_frame&)> routesCb)
{
    return (*impl_)(frame_number, fetch_background, routesCb);
}
core::monitor::state    stage::state() const { return impl_->state_; }
core::video_format_desc stage::video_format_desc() const { return impl_->video_format_desc(); }
std::future<void>       stage::video_format_desc(const core::video_format_desc& format_desc)
{
    return impl_->video_format_desc(format_desc);
}
std::unique_lock<std::mutex> stage::get_lock() const { return impl_->get_lock(); }
std::future<void>            stage::execute(std::function<void()> func)
{
    func();
    return make_ready_future();
}

// STAGE DELAYED (For batching operations)
stage_delayed::stage_delayed(std::shared_ptr<stage>& st, int index)
    : executor_{L"batch stage " + boost::lexical_cast<std::wstring>(index)}
    , stage_(st)
{
    // Start the executor blocked on a future that will complete when we are ready for it to execute
    executor_.begin_invoke([=]() -> void { waiter_.get_future().get(); });
}

std::future<std::wstring> stage_delayed::call(int index, const std::vector<std::wstring>& params)
{
    return executor_.begin_invoke([=]() -> std::wstring { return stage_->call(index, params).get(); });
}
std::future<std::wstring> stage_delayed::callbg(int index, const std::vector<std::wstring>& params)
{
    return executor_.begin_invoke([=]() -> std::wstring { return stage_->callbg(index, params).get(); });
}
std::future<void> stage_delayed::apply_transforms(const std::vector<stage_delayed::transform_tuple_t>& transforms)
{
    return executor_.begin_invoke([=]() { return stage_->apply_transforms(transforms).get(); });
}
std::future<void>
stage_delayed::apply_transform(int                                                                index,
                               const std::function<core::frame_transform(core::frame_transform)>& transform,
                               unsigned int                                                       mix_duration,
                               const tweener&                                                     tween)
{
    return executor_.begin_invoke(
        [=]() { return stage_->apply_transform(index, transform, mix_duration, tween).get(); });
}
std::future<void> stage_delayed::clear_transforms(int index)
{
    return executor_.begin_invoke([=]() { return stage_->clear_transforms(index).get(); });
}
std::future<void> stage_delayed::clear_transforms()
{
    return executor_.begin_invoke([=]() { return stage_->clear_transforms().get(); });
}
std::future<frame_transform> stage_delayed::get_current_transform(int index)
{
    return executor_.begin_invoke([=]() { return stage_->get_current_transform(index).get(); });
}
std::future<void> stage_delayed::load(int                                    index,
                                      const spl::shared_ptr<frame_producer>& producer,
                                      bool                                   preview,
                                      bool                                   auto_play,
                                      item_info                              info)
{
    return executor_.begin_invoke([=]() { return stage_->load(index, producer, preview, auto_play, info).get(); });
}
std::future<void> stage_delayed::invoke_sync(std::function<void(stage_sync_ops&)> func)
{
    return executor_.begin_invoke([=]() { return stage_->invoke_sync(func).get(); });
}
std::future<void> stage_delayed::preview(int index)
{
    return executor_.begin_invoke([=]() { return stage_->preview(index).get(); });
}
std::future<void> stage_delayed::pause(int index)
{
    return executor_.begin_invoke([=]() { return stage_->pause(index).get(); });
}
std::future<void> stage_delayed::resume(int index)
{
    return executor_.begin_invoke([=]() { return stage_->resume(index).get(); });
}
std::future<void> stage_delayed::play(int index)
{
    return executor_.begin_invoke([=]() { return stage_->play(index).get(); });
}
std::future<void> stage_delayed::stop(int index)
{
    return executor_.begin_invoke([=]() { return stage_->stop(index).get(); });
}
std::future<void> stage_delayed::clear(int index)
{
    return executor_.begin_invoke([=]() { return stage_->clear(index).get(); });
}
std::future<void> stage_delayed::clear()
{
    return executor_.begin_invoke([=]() { return stage_->clear().get(); });
}
std::future<void> stage_delayed::swap_layers(const std::shared_ptr<stage_base>& other, bool swap_transforms)
{
    const auto other2 = std::static_pointer_cast<stage_delayed>(other);
    return executor_.begin_invoke([=]() { return stage_->swap_layers(other2->stage_, swap_transforms).get(); });
}
std::future<void> stage_delayed::swap_layer(int index, int other_index, bool swap_transforms)
{
    return executor_.begin_invoke([=]() { return stage_->swap_layer(index, other_index, swap_transforms).get(); });
}
std::future<void>
stage_delayed::swap_layer(int index, int other_index, const std::shared_ptr<stage_base>& other, bool swap_transforms)
{
    const auto other2 = std::static_pointer_cast<stage_delayed>(other);

    // Something so that we know to lock the channel
    other2->executor_.begin_invoke([]() {});

    return executor_.begin_invoke(
        [=]() { return stage_->swap_layer(index, other_index, other2->stage_, swap_transforms).get(); });
}

std::future<std::shared_ptr<frame_producer>> stage_delayed::foreground(int index)
{
    return executor_.begin_invoke([=]() -> std::shared_ptr<frame_producer> { return stage_->foreground(index).get(); });
}
std::future<std::shared_ptr<frame_producer>> stage_delayed::background(int index)
{
    return executor_.begin_invoke([=]() -> std::shared_ptr<frame_producer> { return stage_->background(index).get(); });
}

std::future<void> stage_delayed::execute(std::function<void()> func)
{
    return executor_.begin_invoke([=]() { return stage_->execute(func).get(); });
}

}} // namespace caspar::core
