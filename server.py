import random
import time
import threading
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'snake-secret-2024'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

GRID_W = 30
GRID_H = 25
TICK_RATE = 0.1  # 10fps game loop

PLAYER_COLORS = ['#00FF88', '#FF4466', '#44AAFF', '#FFB800']
PLAYER_NAMES  = ['Snake 1', 'Snake 2', 'Snake 3', 'Snake 4']

rooms = {}   # room_id -> RoomState
room_lock = threading.Lock()


def random_pos(occupied):
    while True:
        p = [random.randint(1, GRID_W - 2), random.randint(1, GRID_H - 2)]
        if p not in occupied:
            return p


class Snake:
    def __init__(self, sid, color, name, start_pos):
        self.sid   = sid
        self.color = color
        self.name  = name
        self.body  = [start_pos, [start_pos[0] - 1, start_pos[1]], [start_pos[0] - 2, start_pos[1]]]
        self.dir   = [1, 0]
        self.next_dir = [1, 0]
        self.alive = True
        self.score = 0
        self.grow  = 0

    def to_dict(self):
        return {
            'sid':   self.sid,
            'color': self.color,
            'name':  self.name,
            'body':  self.body,
            'alive': self.alive,
            'score': self.score,
        }


class RoomState:
    def __init__(self, room_id):
        self.room_id  = room_id
        self.snakes   = {}   # sid -> Snake
        self.food     = []
        self.running  = False
        self.started  = False
        self.thread   = None
        self.color_idx = 0
        self._spawn_food_batch(3)

    def _all_occupied(self):
        occ = []
        for s in self.snakes.values():
            occ += s.body
        occ += self.food
        return occ

    def _spawn_food_batch(self, n=1):
        for _ in range(n):
            if len(self.food) < 6:
                self.food.append(random_pos(self._all_occupied()))

    def add_player(self, sid):
        if len(self.snakes) >= 4:
            return None
        starts = [[4,4],[GRID_W-5,GRID_H-5],[4,GRID_H-5],[GRID_W-5,4]]
        idx    = self.color_idx % 4
        s = Snake(sid, PLAYER_COLORS[idx], PLAYER_NAMES[idx], starts[idx][:])
        self.snakes[sid] = s
        self.color_idx += 1
        return s

    def remove_player(self, sid):
        self.snakes.pop(sid, None)

    def set_dir(self, sid, d):
        s = self.snakes.get(sid)
        if not s or not s.alive:
            return
        dx, dy = d
        # Prevent 180 reversal
        if dx == -s.dir[0] and dy == -s.dir[1]:
            return
        s.next_dir = [dx, dy]

    def step(self):
        heads_after = {}
        for s in self.snakes.values():
            if not s.alive:
                continue
            s.dir = s.next_dir[:]
            new_head = [s.body[0][0] + s.dir[0], s.body[0][1] + s.dir[1]]

            # Wall collision
            if new_head[0] < 0 or new_head[0] >= GRID_W or new_head[1] < 0 or new_head[1] >= GRID_H:
                s.alive = False
                continue

            # Self collision
            check_body = s.body[:-1] if s.grow == 0 else s.body
            if new_head in check_body:
                s.alive = False
                continue

            heads_after[s.sid] = new_head

        # Head-to-head collision
        head_positions = list(heads_after.values())
        for sid, head in heads_after.items():
            if head_positions.count(head) > 1:
                self.snakes[sid].alive = False

        for s in self.snakes.values():
            if not s.alive or s.sid not in heads_after:
                continue
            new_head = heads_after[s.sid]

            # Other snake body collision
            for other in self.snakes.values():
                if other.sid == s.sid:
                    continue
                if new_head in other.body:
                    s.alive = False
                    break
            if not s.alive:
                continue

            # Move
            s.body.insert(0, new_head)
            if s.grow > 0:
                s.grow -= 1
            else:
                s.body.pop()

            # Food
            if new_head in self.food:
                self.food.remove(new_head)
                s.score += 1
                s.grow  += 3
                self._spawn_food_batch(1)

    def state_dict(self):
        return {
            'snakes': {sid: s.to_dict() for sid, s in self.snakes.items()},
            'food':   self.food,
            'grid':   [GRID_W, GRID_H],
        }

    def all_dead(self):
        return all(not s.alive for s in self.snakes.values())


def game_loop(room_id):
    room = rooms.get(room_id)
    if not room:
        return
    while room.running and room.snakes:
        t0 = time.time()
        with room_lock:
            room.step()
            state = room.state_dict()
            game_over = room.all_dead()
        socketio.emit('state', state, room=room_id)
        if game_over:
            socketio.emit('game_over', {
                'scores': {s.name: s.score for s in room.snakes.values()}
            }, room=room_id)
            room.running = False
            break
        elapsed = time.time() - t0
        time.sleep(max(0, TICK_RATE - elapsed))


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('connect')
def on_connect():
    print(f'Client connected: {request.sid}')


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    with room_lock:
        for rid, room in list(rooms.items()):
            if sid in room.snakes:
                room.remove_player(sid)
                socketio.emit('player_left', {'sid': sid}, room=rid)
                if not room.snakes:
                    room.running = False
                    del rooms[rid]
                break


@socketio.on('join')
def on_join(data):
    room_id = data.get('room', 'lobby').strip() or 'lobby'
    sid = request.sid
    with room_lock:
        if room_id not in rooms:
            rooms[room_id] = RoomState(room_id)
        room = rooms[room_id]
        if len(room.snakes) >= 4:
            emit('error', {'msg': 'Room is full (max 4 players)'})
            return
        snake = room.add_player(sid)
    join_room(room_id)
    emit('joined', {
        'sid':      sid,
        'color':    snake.color,
        'name':     snake.name,
        'room':     room_id,
        'players':  [s.to_dict() for s in room.snakes.values()],
        'grid':     [GRID_W, GRID_H],
    })
    socketio.emit('player_joined', {'player': snake.to_dict()}, room=room_id, skip_sid=sid)


@socketio.on('start')
def on_start(data):
    room_id = data.get('room', 'lobby')
    sid = request.sid
    with room_lock:
        room = rooms.get(room_id)
        if not room or sid not in room.snakes:
            return
        if room.running:
            return
        room.running = True
        room.started = True
        t = threading.Thread(target=game_loop, args=(room_id,), daemon=True)
        room.thread = t
    socketio.emit('game_started', {}, room=room_id)
    t.start()


@socketio.on('dir')
def on_dir(data):
    room_id = data.get('room')
    d = data.get('d', [1, 0])
    sid = request.sid
    with room_lock:
        room = rooms.get(room_id)
        if room:
            room.set_dir(sid, d)


@socketio.on('restart')
def on_restart(data):
    room_id = data.get('room', 'lobby')
    with room_lock:
        if room_id in rooms:
            old = rooms[room_id]
            old.running = False
            new_room = RoomState(room_id)
            for sid, s in old.snakes.items():
                new_s = new_room.add_player(sid)
            rooms[room_id] = new_room
            state = new_room.state_dict()
            players = [s.to_dict() for s in new_room.snakes.values()]
    socketio.emit('restarted', {'players': players, 'state': state}, room=room_id)


if __name__ == '__main__':
    print("🐍 Snake Multiplayer Server running on http://localhost:5000")
    print("   Share your local IP with friends on the same network.")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
