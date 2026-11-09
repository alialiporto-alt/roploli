import os, sqlite3, secrets, string, logging
from datetime import datetime
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters)

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '').strip()
DB_PATH = os.getenv('DB_PATH', 'exam_bot.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('exam-bot')

(STUDENT_CODE, EXAM_CODE, ADMIN_PASS, BULK_NAMES, EDIT_CODE, EDIT_NAME,
 DELETE_CODE, EXAM_TITLE, EXAM_SHARED_CODE, EXAM_START, EXAM_END, EXAM_DURATION,
 Q_EXAM, Q_TEXT, Q_TYPE, Q_OPTIONS, Q_ANSWER, Q_POINTS, GRADE_VALUE) = range(19)
EXAM_TIMEOUT_JOB = 'exam_timeout'


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def now():
    return datetime.now().astimezone()


def stamp(dt=None):
    return (dt or now()).isoformat(timespec='seconds')


def parse_dt(value):
    return datetime.strptime(value.strip(), '%Y-%m-%d %H:%M').astimezone()


def make_code(n=9):
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(n))


def fresh_code(table, column='access_code', n=9):
    while True:
        value = make_code(n)
        with db() as c:
            if not c.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (value,)).fetchone():
                return value


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS students(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          access_code TEXT UNIQUE NOT NULL, code_used INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS exams(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          exam_code TEXT NOT NULL UNIQUE, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
          duration_minutes INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS questions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
          position INTEGER NOT NULL, qtype TEXT NOT NULL CHECK(qtype IN ('mcq','essay')),
          body TEXT NOT NULL, options TEXT, answer TEXT, points REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER NOT NULL REFERENCES exams(id),
          student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
          started_at TEXT NOT NULL, submitted_at TEXT, status TEXT NOT NULL DEFAULT 'in_progress',
          score REAL NOT NULL DEFAULT 0, UNIQUE(exam_id, student_id));
        CREATE TABLE IF NOT EXISTS answers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
          question_id INTEGER NOT NULL REFERENCES questions(id), answer TEXT,
          awarded_points REAL, UNIQUE(attempt_id, question_id));
        CREATE TABLE IF NOT EXISTS admin_sessions(
          telegram_id INTEGER PRIMARY KEY, logged_in INTEGER NOT NULL DEFAULT 0);
        ''')


    # Safely rebuild legacy v1 student table without the Telegram-ID column.
    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        con.execute('PRAGMA foreign_keys=OFF')
        cols = [r[1] for r in con.execute('PRAGMA table_info(students)').fetchall()]
        if 'telegram_id' in cols:
            con.execute("CREATE TABLE students_new(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, access_code TEXT UNIQUE NOT NULL, code_used INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
            con.execute("INSERT INTO students_new(id,name,access_code,code_used,created_at) SELECT id,name,access_code,CASE WHEN telegram_id IS NOT NULL THEN 1 ELSE 0 END,created_at FROM students")
            con.execute('DROP TABLE students')
            con.execute('ALTER TABLE students_new RENAME TO students')
            con.commit()
    finally:
        con.close()

def is_admin(update):
    return bool(update.effective_user and ADMIN_ID and update.effective_user.id == ADMIN_ID)


def logged_admin(uid):
    with db() as c:
        r = c.execute('SELECT logged_in FROM admin_sessions WHERE telegram_id=?', (uid,)).fetchone()
    return bool(r and r['logged_in'])


def admin_guard(fn):
    @wraps(fn)
    async def wrapped(update, context, *args, **kwargs):
        if not is_admin(update) or not logged_admin(update.effective_user.id):
            if update.callback_query:
                await update.callback_query.answer('دسترسی مدیریت ندارید.', show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text('⛔ ابتدا با حساب مدیر وارد شوید.')
            return ConversationHandler.END
        return await fn(update, context, *args, **kwargs)
    return wrapped


def main_kb():
    return InlineKeyboardMarkup([
      [InlineKeyboardButton('🎓 ورود با کد دانشجویی', callback_data='student_login')],
      [InlineKeyboardButton('📝 شرکت در آزمون', callback_data='exam_enter')],
      [InlineKeyboardButton('📊 کارنامه من', callback_data='scores')],
      [InlineKeyboardButton('🔐 پنل مدیریت', callback_data='admin_login')]])


def admin_kb():
    return InlineKeyboardMarkup([
      [InlineKeyboardButton('➕ ثبت گروهی دانشجو', callback_data='bulk')],
      [InlineKeyboardButton('✏️ ویرایش دانشجو', callback_data='edit_student'), InlineKeyboardButton('🗑 حذف دانشجو', callback_data='delete_student')],
      [InlineKeyboardButton('🔓 فعال‌سازی مجدد کد دانشجو', callback_data='reactivate_student')],
      [InlineKeyboardButton('♻️ بازنشانی تلاش آزمون', callback_data='reset_attempt')],
      [InlineKeyboardButton('🧪 ساخت آزمون', callback_data='new_exam')],
      [InlineKeyboardButton('➕ افزودن سؤال', callback_data='add_question')],
      [InlineKeyboardButton('🔑 نمایش کدهای آزمون', callback_data='codes')],
      [InlineKeyboardButton('📊 نتایج و تصحیح تشریحی', callback_data='results')],
      [InlineKeyboardButton('🏠 منوی اصلی', callback_data='home')]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text('🎓 به سامانه آزمون آنلاین خوش آمدید.\nبرای شروع یکی از گزینه‌ها را انتخاب کنید:', reply_markup=main_kb())


async def home(update, context):
    q = update.callback_query; await q.answer()
    await q.message.reply_text('🏠 منوی اصلی', reply_markup=main_kb())
    return ConversationHandler.END


async def student_login_start(update, context):
    q = update.callback_query; await q.answer()
    await q.message.reply_text('کد اختصاصی دانشجویی خود را وارد کنید:')
    return STUDENT_CODE


async def student_code_received(update, context):
    code = update.message.text.strip().upper()
    with db() as c:
        student = c.execute('SELECT * FROM students WHERE access_code=?', (code,)).fetchone()
        if not student:
            await update.message.reply_text('❌ کد دانشجویی پیدا نشد. دوباره تلاش کنید یا با مدیر تماس بگیرید.')
            return STUDENT_CODE
        if student['code_used']:
            await update.message.reply_text('⚠️ این کد قبلاً استفاده شده است. برای فعال‌سازی مجدد باید با مدیر تماس بگیرید.')
            return ConversationHandler.END
        c.execute('UPDATE students SET code_used=1 WHERE id=? AND code_used=0', (student['id'],))
    # Student identity is kept only in the active bot session; no student Telegram ID is stored in the database.
    context.user_data['student_id'] = student['id']
    await update.message.reply_text(f"✅ خوش آمدی {student['name']}!\nکد دانشجویی شما مصرف شد. حالا می‌توانید کد مشترک آزمون را وارد کنید.", reply_markup=main_kb())
    return ConversationHandler.END


async def exam_enter_start(update, context):
    q = update.callback_query; await q.answer()
    if not context.user_data.get('student_id'):
        await q.message.reply_text('ابتدا با کد دانشجویی وارد شوید. پس از ورود، کد دانشجو یک‌بارمصرف می‌شود.')
        return ConversationHandler.END
    await q.message.reply_text('کد مشترک آزمون را وارد کنید:')
    return EXAM_CODE


async def exam_code_received(update, context):
    code = update.message.text.strip().upper()
    student_id = context.user_data.get('student_id')
    if not student_id:
        await update.message.reply_text('نشست دانشجویی فعال نیست؛ با مدیر تماس بگیرید تا کد دانشجو را دوباره فعال کند.')
        return ConversationHandler.END
    with db() as c:
        exam = c.execute('SELECT * FROM exams WHERE exam_code=?', (code,)).fetchone()
        if not exam or not exam['active']:
            await update.message.reply_text('❌ کد آزمون نامعتبر است یا آزمون غیرفعال شده است.')
            return ConversationHandler.END
        t = now(); start_dt = datetime.fromisoformat(exam['starts_at']); end_dt = datetime.fromisoformat(exam['ends_at'])
        if t < start_dt or t > end_dt:
            await update.message.reply_text('⏰ آزمون هنوز شروع نشده یا مهلتش تمام شده است.')
            return ConversationHandler.END
        existing = c.execute('SELECT id,status FROM attempts WHERE exam_id=? AND student_id=?', (exam['id'], student_id)).fetchone()
        if existing:
            await update.message.reply_text('⚠️ شما قبلاً در این آزمون شرکت کرده‌اید. برای تلاش مجدد باید مدیر تلاش قبلی را بازنشانی کند.')
            return ConversationHandler.END
        questions = c.execute('SELECT * FROM questions WHERE exam_id=? ORDER BY position,id', (exam['id'],)).fetchall()
        if not questions:
            await update.message.reply_text('این آزمون هنوز سؤالی ندارد.')
            return ConversationHandler.END
        cur = c.execute('INSERT INTO attempts(exam_id,student_id,started_at) VALUES(?,?,?)', (exam['id'], student_id, stamp(t)))
        attempt_id = cur.lastrowid
    context.user_data.update({'attempt_id': attempt_id, 'exam_id': exam['id'], 'questions': [dict(x) for x in questions], 'q_index': 0,
                              'deadline': min(t.timestamp()+exam['duration_minutes']*60, end_dt.timestamp()), 'awaiting_essay': None})
    # Deadline job auto-submits even if the student sends no further messages.
    context.job_queue.run_once(exam_timeout, max(1, context.user_data['deadline']-t.timestamp()),
        data={'attempt_id': attempt_id, 'chat_id': update.effective_chat.id}, name=f'{EXAM_TIMEOUT_JOB}_{attempt_id}')
    await update.message.reply_text(f"✅ آزمون «{exam['title']}» شروع شد.\nمدت: {exam['duration_minutes']} دقیقه\nبرای خروج، دکمه پایان آزمون را بزنید.")
    await send_question(update, context)
    return Q_EXAM


async def send_question(update, context):
    qs = context.user_data.get('questions', []); i = context.user_data.get('q_index', 0)
    if i >= len(qs):
        await finish_attempt(update, context, 'submitted'); return
    q = qs[i]
    body = f"سؤال {i+1} از {len(qs)}\n\n{q['body']}\n\nنمره: {q['points']}"
    finish = [InlineKeyboardButton('🏁 پایان آزمون', callback_data='finish')]
    if q['qtype'] == 'mcq':
        opts = (q['options'] or '').split('|')
        rows = [[InlineKeyboardButton(f'{chr(65+j)}) {op}', callback_data=f"answer_{q['id']}_{j}")] for j, op in enumerate(opts)]
        rows.append(finish)
        await update.effective_message.reply_text(body, reply_markup=InlineKeyboardMarkup(rows))
    else:
        context.user_data['awaiting_essay'] = q['id']
        await update.effective_message.reply_text(body+'\n\nپاسخ تشریحی را در یک پیام ارسال کنید.', reply_markup=InlineKeyboardMarkup([finish]))


async def answer_button(update, context):
    q = update.callback_query; await q.answer()
    if not context.user_data.get('attempt_id'): return ConversationHandler.END
    if now().timestamp() >= context.user_data.get('deadline', 0):
        await finish_attempt(update, context, 'auto_submitted'); return ConversationHandler.END
    if q.data == 'finish':
        await finish_attempt(update, context, 'submitted'); return ConversationHandler.END
    _, qid, idx = q.data.split('_'); qid=int(qid); idx=int(idx)
    question = next((x for x in context.user_data['questions'] if x['id']==qid), None)
    if not question: return Q_EXAM
    options=(question['options'] or '').split('|')
    if idx >= len(options): return Q_EXAM
    letter=chr(65+idx); correct=(question['answer'] or '').strip().upper()
    points=float(question['points']) if letter==correct else 0.0
    with db() as c:
        c.execute('INSERT OR REPLACE INTO answers(attempt_id,question_id,answer,awarded_points) VALUES(?,?,?,?)',
                  (context.user_data['attempt_id'], qid, letter, points))
    context.user_data['q_index'] += 1
    await q.message.reply_text('پاسخ ثبت شد.')
    await send_question(update, context)
    return Q_EXAM


async def essay_received(update, context):
    qid=context.user_data.get('awaiting_essay')
    if not qid or not context.user_data.get('attempt_id'): return ConversationHandler.END
    if now().timestamp() >= context.user_data.get('deadline', 0):
        await finish_attempt(update, context, 'auto_submitted'); return ConversationHandler.END
    with db() as c:
        c.execute('INSERT OR REPLACE INTO answers(attempt_id,question_id,answer,awarded_points) VALUES(?,?,?,NULL)',
                  (context.user_data['attempt_id'], qid, update.message.text))
    context.user_data['awaiting_essay']=None; context.user_data['q_index']+=1
    await update.message.reply_text('پاسخ ثبت شد.')
    await send_question(update, context)
    return Q_EXAM


async def finish_attempt(update, context, status='submitted', notify=True):
    aid=context.user_data.get('attempt_id')
    if not aid: return
    with db() as c:
        total=c.execute('SELECT COALESCE(SUM(awarded_points),0) score FROM answers WHERE attempt_id=?', (aid,)).fetchone()['score']
        c.execute('UPDATE attempts SET submitted_at=?,status=?,score=? WHERE id=? AND status="in_progress"', (stamp(),status,total,aid))
    if notify:
        msg=f'🏁 آزمون بسته شد.\nنمره تستی ثبت‌شده: {total}\nپاسخ‌های تشریحی پس از تصحیح مدیر به نمره اضافه می‌شوند.'
        if update.callback_query: await update.callback_query.message.reply_text(msg)
        elif update.effective_message: await update.effective_message.reply_text(msg)
    job=context.job_queue.get_jobs_by_name(f'{EXAM_TIMEOUT_JOB}_{aid}')
    for j in job: j.schedule_removal()
    for key in ('attempt_id','exam_id','questions','q_index','deadline','awaiting_essay'):
        context.user_data.pop(key,None)


async def exam_timeout(context: ContextTypes.DEFAULT_TYPE):
    data=context.job.data; aid=data['attempt_id']
    with db() as c:
        row=c.execute('SELECT status,score FROM attempts WHERE id=?', (aid,)).fetchone()
        if not row or row['status']!='in_progress': return
        score=c.execute('SELECT COALESCE(SUM(awarded_points),0) s FROM answers WHERE attempt_id=?',(aid,)).fetchone()['s']
        c.execute('UPDATE attempts SET submitted_at=?,status="auto_submitted",score=? WHERE id=?', (stamp(),score,aid))
    try:
        await context.bot.send_message(data['chat_id'], f'⏰ زمان آزمون تمام شد و پاسخ‌های ثبت‌شده ذخیره شدند. نمره تستی فعلی: {score}')
    except Exception: log.exception('Could not notify exam timeout')


async def scores(update, context):
    q=update.callback_query; await q.answer()
    sid=context.user_data.get('student_id')
    if not sid:
        await q.message.reply_text('ابتدا با کد دانشجویی وارد شوید.')
        return
    with db() as c:
        rows=c.execute('''SELECT e.title,a.score,a.status,
          (SELECT COUNT(*) FROM answers an JOIN questions qu ON qu.id=an.question_id WHERE an.attempt_id=a.id AND qu.qtype='essay' AND an.awarded_points IS NULL) pending
          FROM attempts a JOIN exams e ON e.id=a.exam_id WHERE a.student_id=? ORDER BY a.id DESC''',(sid,)).fetchall()
    if not rows: await q.message.reply_text('هنوز نمره‌ای ثبت نشده است.'); return
    lines=[f"📘 {r['title']} — {r['score']} نمره" + (f" | {r['pending']} پاسخ تشریحی در انتظار" if r['pending'] else '') for r in rows]
    await q.message.reply_text('📊 کارنامه شما\n\n'+'\n'.join(lines))


async def admin_login_start(update, context):
    q=update.callback_query; await q.answer()
    if not is_admin(update):
        await q.message.reply_text('⛔ این حساب، مدیر ثبت‌شده نیست.'); return ConversationHandler.END
    await q.message.reply_text('رمز مدیریت را وارد کنید:'); return ADMIN_PASS


async def admin_password(update, context):
    if not ADMIN_PASSWORD or update.message.text.strip()!=ADMIN_PASSWORD:
        await update.message.reply_text('❌ رمز اشتباه است.'); return ConversationHandler.END
    with db() as c:
        c.execute('INSERT OR REPLACE INTO admin_sessions(telegram_id,logged_in) VALUES(?,1)', (update.effective_user.id,))
    await update.message.reply_text('✅ ورود مدیر موفق بود.', reply_markup=admin_kb()); return ConversationHandler.END


@admin_guard
async def admin_bulk_start(update, context):
    q=update.callback_query; await q.answer(); await q.message.reply_text('نام هر دانشجو را در یک خط بفرستید:'); return BULK_NAMES


@admin_guard
async def admin_bulk_save(update, context):
    names=[x.strip() for x in update.message.text.splitlines() if x.strip()]
    if not names: await update.message.reply_text('حداقل یک نام وارد کنید.'); return BULK_NAMES
    made=[]
    with db() as c:
        for name in names:
            code=fresh_code('students')
            c.execute('INSERT INTO students(name,access_code,created_at) VALUES(?,?,?)',(name,code,stamp()))
            made.append((name,code))
    await update.message.reply_text('✅ کدهای دانشجویان ساخته شد:\n\n'+'\n'.join(f'{n} — {code}' for n,code in made), reply_markup=admin_kb())
    return ConversationHandler.END


@admin_guard
async def student_edit_start(update, context):
    q=update.callback_query; await q.answer(); await q.message.reply_text('کد دانشجویی فرد موردنظر را وارد کنید:'); return EDIT_CODE


@admin_guard
async def student_edit_code(update, context):
    code=update.message.text.strip().upper()
    with db() as c: s=c.execute('SELECT id,name FROM students WHERE access_code=?',(code,)).fetchone()
    if not s: await update.message.reply_text('دانشجو پیدا نشد. دوباره کد را وارد کنید:'); return EDIT_CODE
    context.user_data['edit_student_id']=s['id']
    await update.message.reply_text(f"نام فعلی: {s['name']}\nنام جدید را وارد کنید:"); return EDIT_NAME


@admin_guard
async def student_edit_name(update, context):
    name=update.message.text.strip()
    if not name: await update.message.reply_text('نام خالی است؛ دوباره وارد کنید:'); return EDIT_NAME
    with db() as c: c.execute('UPDATE students SET name=? WHERE id=?',(name,context.user_data.pop('edit_student_id')))
    await update.message.reply_text('✅ نام دانشجو ویرایش شد.',reply_markup=admin_kb()); return ConversationHandler.END


@admin_guard
async def student_delete_start(update, context):
    q=update.callback_query; await q.answer(); await q.message.reply_text('کد دانشجویی فردی را که باید حذف شود وارد کنید. این کار پاسخ‌ها و نمره‌های او را هم حذف می‌کند:'); return DELETE_CODE


@admin_guard
async def student_delete_code(update, context):
    code=update.message.text.strip().upper()
    with db() as c:
        s=c.execute('SELECT id,name FROM students WHERE access_code=?',(code,)).fetchone()
        if not s: await update.message.reply_text('دانشجو پیدا نشد. کد را دوباره وارد کنید:'); return DELETE_CODE
        c.execute('DELETE FROM students WHERE id=?',(s['id'],))
    await update.message.reply_text(f"🗑 دانشجوی «{s['name']}» و سوابق وابسته حذف شد.",reply_markup=admin_kb()); return ConversationHandler.END


@admin_guard
async def reactivate_student_start(update, context):
    q=update.callback_query; await q.answer(); await q.message.reply_text('کد دانشجویی را برای فعال‌سازی مجدد وارد کنید:'); return EDIT_CODE


@admin_guard
async def reactivate_student_code(update, context):
    code=update.message.text.strip().upper()
    with db() as c: cur=c.execute('UPDATE students SET code_used=0 WHERE access_code=?',(code,))
    if cur.rowcount: await update.message.reply_text('✅ کد دانشجو دوباره فعال شد.',reply_markup=admin_kb())
    else: await update.message.reply_text('❌ کدی با این مشخصات پیدا نشد.')
    return ConversationHandler.END


@admin_guard
async def new_exam_start(update, context):
    q=update.callback_query; await q.answer(); await q.message.reply_text('عنوان آزمون را وارد کنید:'); return EXAM_TITLE


@admin_guard
async def new_exam_title(update, context):
    context.user_data['new_exam']={'title':update.message.text.strip()}
    await update.message.reply_text('کد مشترک آزمون را وارد کنید (همین کد را همه دانشجوها وارد می‌کنند):'); return EXAM_SHARED_CODE


@admin_guard
async def new_exam_shared_code(update, context):
    code=update.message.text.strip().upper()
    if len(code)<3: await update.message.reply_text('کد باید حداقل ۳ نویسه باشد. دوباره وارد کنید:'); return EXAM_SHARED_CODE
    with db() as c:
        if c.execute('SELECT 1 FROM exams WHERE exam_code=?',(code,)).fetchone():
            await update.message.reply_text('این کد قبلاً برای آزمون دیگری ثبت شده؛ کد دیگری وارد کنید:'); return EXAM_SHARED_CODE
    context.user_data['new_exam']['exam_code']=code
    await update.message.reply_text('زمان شروع را به شکل YYYY-MM-DD HH:MM وارد کنید:'); return EXAM_START


@admin_guard
async def new_exam_start_dt(update, context):
    try: context.user_data['new_exam']['starts_at']=stamp(parse_dt(update.message.text))
    except ValueError: await update.message.reply_text('فرمت اشتباه است؛ نمونه: 2026-11-20 09:00'); return EXAM_START
    await update.message.reply_text('زمان پایان را به شکل YYYY-MM-DD HH:MM وارد کنید:'); return EXAM_END


@admin_guard
async def new_exam_end_dt(update, context):
    try:
        end=parse_dt(update.message.text); start_dt=datetime.fromisoformat(context.user_data['new_exam']['starts_at'])
        if end<=start_dt: raise ValueError()
        context.user_data['new_exam']['ends_at']=stamp(end)
    except ValueError: await update.message.reply_text('زمان پایان باید بعد از شروع باشد. نمونه: 2026-11-20 12:00'); return EXAM_END
    await update.message.reply_text('مدت آزمون به دقیقه را وارد کنید:'); return EXAM_DURATION


@admin_guard
async def new_exam_duration(update, context):
    try:
        minutes=int(update.message.text.strip())
        if not 1<=minutes<=600: raise ValueError()
    except ValueError: await update.message.reply_text('عدد صحیح بین 1 تا 600 وارد کنید:'); return EXAM_DURATION
    d=context.user_data.pop('new_exam')
    with db() as c: c.execute('INSERT INTO exams(title,exam_code,starts_at,ends_at,duration_minutes,created_at) VALUES(?,?,?,?,?,?)',
        (d['title'],d['exam_code'],d['starts_at'],d['ends_at'],minutes,stamp()))
    await update.message.reply_text(f"✅ آزمون ساخته شد.\nعنوان: {d['title']}\nکد مشترک: {d['exam_code']}\nهمه دانشجوها از همین کد استفاده می‌کنند.\nبرای افزودن سؤال از دکمه «افزودن سؤال» استفاده کنید.",reply_markup=admin_kb())
    return ConversationHandler.END


@admin_guard
async def add_question_start(update, context):
    q=update.callback_query; await q.answer()
    with db() as c: exams=c.execute('SELECT id,title FROM exams ORDER BY id DESC').fetchall()
    if not exams: await q.message.reply_text('ابتدا آزمون بسازید.'); return ConversationHandler.END
    kb=[[InlineKeyboardButton(e['title'],callback_data=f"qexam_{e['id']}")] for e in exams]
    await q.message.reply_text('آزمون را انتخاب کنید:',reply_markup=InlineKeyboardMarkup(kb)); return Q_EXAM


@admin_guard
async def question_exam_selected(update, context):
    q=update.callback_query; await q.answer(); context.user_data['question_exam_id']=int(q.data.split('_')[1])
    await q.message.reply_text('متن سؤال را وارد کنید:'); return Q_TEXT


@admin_guard
async def question_text(update, context):
    context.user_data['question_body']=update.message.text.strip()
    await update.message.reply_text('نوع سؤال را بنویسید: تستی یا تشریحی'); return Q_TYPE


@admin_guard
async def question_type(update, context):
    t=update.message.text.strip()
    if t not in ('تستی','تشریحی'): await update.message.reply_text('فقط «تستی» یا «تشریحی»:'); return Q_TYPE
    context.user_data['question_type']='mcq' if t=='تستی' else 'essay'
    if t=='تستی': await update.message.reply_text('چهار گزینه را در چهار خط وارد کنید:'); return Q_OPTIONS
    await update.message.reply_text('نمره سؤال را وارد کنید:'); return Q_POINTS


@admin_guard
async def question_options(update, context):
    opts=[x.strip() for x in update.message.text.splitlines() if x.strip()]
    if len(opts)!=4: await update.message.reply_text('دقیقاً چهار گزینه در چهار خط وارد کنید:'); return Q_OPTIONS
    context.user_data['question_options']='|'.join(opts)
    await update.message.reply_text('پاسخ صحیح را با A یا B یا C یا D وارد کنید:'); return Q_ANSWER


@admin_guard
async def question_answer(update, context):
    a=update.message.text.strip().upper()
    if a not in ('A','B','C','D'): await update.message.reply_text('فقط A یا B یا C یا D:'); return Q_ANSWER
    context.user_data['question_answer']=a
    await update.message.reply_text('نمره سؤال را وارد کنید:'); return Q_POINTS


@admin_guard
async def question_points(update, context):
    try: points=float(update.message.text.strip()); assert points>0
    except Exception: await update.message.reply_text('نمره باید عددی بزرگ‌تر از صفر باشد:'); return Q_POINTS
    d=context.user_data
    with db() as c:
        pos=c.execute('SELECT COALESCE(MAX(position),0)+1 p FROM questions WHERE exam_id=?',(d['question_exam_id'],)).fetchone()['p']
        c.execute('INSERT INTO questions(exam_id,position,qtype,body,options,answer,points) VALUES(?,?,?,?,?,?,?)',
          (d['question_exam_id'],pos,d['question_type'],d['question_body'],d.get('question_options'),d.get('question_answer'),points))
    for k in ('question_exam_id','question_body','question_type','question_options','question_answer'): d.pop(k,None)
    await update.message.reply_text('✅ سؤال ثبت شد.',reply_markup=admin_kb()); return ConversationHandler.END


@admin_guard
async def reset_attempt_start(update, context):
    q=update.callback_query; await q.answer()
    await q.message.reply_text('کد دانشجویی را وارد کنید تا تلاش آزمون او بازنشانی شود:')
    return DELETE_CODE


@admin_guard
async def reset_attempt_student(update, context):
    code=update.message.text.strip().upper()
    with db() as c:
        student=c.execute('SELECT id,name FROM students WHERE access_code=?',(code,)).fetchone()
    if not student:
        await update.message.reply_text('دانشجو پیدا نشد؛ دوباره کد دانشجو را وارد کنید:')
        return DELETE_CODE
    context.user_data['reset_student_id']=student['id']
    await update.message.reply_text('حالا کد مشترک آزمون را وارد کنید:')
    return EDIT_NAME


@admin_guard
async def reset_attempt_exam(update, context):
    code=update.message.text.strip().upper(); sid=context.user_data.pop('reset_student_id',None)
    with db() as c:
        exam=c.execute('SELECT id,title FROM exams WHERE exam_code=?',(code,)).fetchone()
        if not exam:
            await update.message.reply_text('آزمونی با این کد پیدا نشد. دوباره کد آزمون را وارد کنید:')
            context.user_data['reset_student_id']=sid
            return EDIT_NAME
        cur=c.execute('DELETE FROM attempts WHERE student_id=? AND exam_id=?',(sid,exam['id']))
    if cur.rowcount:
        await update.message.reply_text('✅ تلاش قبلی و پاسخ‌های آن حذف شد؛ دانشجو می‌تواند دوباره در آزمون شرکت کند.',reply_markup=admin_kb())
    else:
        await update.message.reply_text('برای این دانشجو در این آزمون تلاشی ثبت نشده است.',reply_markup=admin_kb())
    return ConversationHandler.END


@admin_guard
async def show_codes(update, context):
    q=update.callback_query; await q.answer()
    with db() as c: rows=c.execute('SELECT title,exam_code,starts_at,ends_at,active FROM exams ORDER BY id DESC').fetchall()
    if not rows: await q.message.reply_text('هنوز آزمونی ثبت نشده است.'); return
    await q.message.reply_text('🔑 کدهای مشترک آزمون:\n\n'+'\n'.join(f"{r['title']} | {r['exam_code']} | {r['starts_at']} تا {r['ends_at']} | {'فعال' if r['active'] else 'غیرفعال'}" for r in rows))


@admin_guard
async def show_results(update, context):
    q=update.callback_query; await q.answer()
    with db() as c:
        rows=c.execute('''SELECT a.id,e.title,s.name,a.score,a.status,
          (SELECT COUNT(*) FROM answers an JOIN questions qu ON qu.id=an.question_id WHERE an.attempt_id=a.id AND qu.qtype='essay' AND an.awarded_points IS NULL) pending
          FROM attempts a JOIN exams e ON e.id=a.exam_id JOIN students s ON s.id=a.student_id ORDER BY a.id DESC LIMIT 50''').fetchall()
    if not rows: await q.message.reply_text('هنوز آزمونی ثبت نشده است.'); return
    context.user_data['result_rows']=[dict(r) for r in rows]
    await q.message.reply_text('📊 نتایج اخیر:\n'+'\n'.join(f"#{r['id']} {r['name']} | {r['title']} | {r['score']} | تشریحیِ بررسی‌نشده: {r['pending']}" for r in rows)+'\n\nبرای تصحیح پاسخ‌های تشریحی دستور /grade را بزنید.')


@admin_guard
async def grade_start(update, context):
    with db() as c:
        rows=c.execute('''SELECT an.id answer_id,a.id attempt_id,s.name,e.title,q.body,an.answer,q.points
          FROM answers an JOIN attempts a ON a.id=an.attempt_id JOIN students s ON s.id=a.student_id
          JOIN exams e ON e.id=a.exam_id JOIN questions q ON q.id=an.question_id
          WHERE q.qtype='essay' AND an.awarded_points IS NULL ORDER BY an.id''').fetchall()
    if not rows: await update.message.reply_text('پاسخ تشریحی بررسی‌نشده‌ای وجود ندارد.'); return ConversationHandler.END
    context.user_data['grade_queue']=[dict(r) for r in rows]; context.user_data['grade_index']=0
    await show_grade(update,context); return GRADE_VALUE


async def show_grade(update, context):
    r=context.user_data['grade_queue'][context.user_data['grade_index']]
    await update.effective_message.reply_text(f"تصحیح {context.user_data['grade_index']+1} از {len(context.user_data['grade_queue'])}\nدانشجو: {r['name']}\nآزمون: {r['title']}\nسؤال: {r['body']}\nپاسخ: {r['answer']}\nحداکثر نمره: {r['points']}\n\nنمره اعطایی را وارد کنید:")


@admin_guard
async def grade_value(update, context):
    try:
        r=context.user_data['grade_queue'][context.user_data['grade_index']]
        val=float(update.message.text.strip())
        if val<0 or val>float(r['points']): raise ValueError()
    except Exception: await update.message.reply_text('نمره نامعتبر است؛ عددی بین صفر و حداکثر سؤال وارد کنید:'); return GRADE_VALUE
    with db() as c:
        c.execute('UPDATE answers SET awarded_points=? WHERE id=?',(val,r['answer_id']))
        score=c.execute('SELECT COALESCE(SUM(awarded_points),0) s FROM answers WHERE attempt_id=?',(r['attempt_id'],)).fetchone()['s']
        c.execute('UPDATE attempts SET score=? WHERE id=?',(score,r['attempt_id']))
    context.user_data['grade_index']+=1
    if context.user_data['grade_index']>=len(context.user_data['grade_queue']):
        await update.message.reply_text('✅ همه پاسخ‌های تشریحی بررسی شدند.',reply_markup=admin_kb()); return ConversationHandler.END
    await update.message.reply_text('نمره ثبت شد.'); await show_grade(update,context); return GRADE_VALUE


async def cancel(update, context):
    await update.message.reply_text('عملیات لغو شد.',reply_markup=main_kb()); return ConversationHandler.END


def main():
    if not BOT_TOKEN: raise RuntimeError('BOT_TOKEN is missing')
    if not ADMIN_ID or not ADMIN_PASSWORD: raise RuntimeError('ADMIN_ID and ADMIN_PASSWORD must be set')
    init_db()
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('cancel',cancel))

    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(student_login_start,pattern='^student_login$')],states={STUDENT_CODE:[MessageHandler(filters.TEXT & ~filters.COMMAND,student_code_received)]},fallbacks=[CommandHandler('cancel',cancel)],allow_reentry=True))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(exam_enter_start,pattern='^exam_enter$')],states={EXAM_CODE:[MessageHandler(filters.TEXT & ~filters.COMMAND,exam_code_received)],Q_EXAM:[CallbackQueryHandler(answer_button,pattern='^(answer_|finish)')]},fallbacks=[CommandHandler('cancel',cancel)],allow_reentry=True))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(admin_login_start,pattern='^admin_login$')],states={ADMIN_PASS:[MessageHandler(filters.TEXT & ~filters.COMMAND,admin_password)]},fallbacks=[CommandHandler('cancel',cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(admin_bulk_start,pattern='^bulk$')],states={BULK_NAMES:[MessageHandler(filters.TEXT & ~filters.COMMAND,admin_bulk_save)]},fallbacks=[CommandHandler('cancel',cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(student_edit_start,pattern='^edit_student$')],states={EDIT_CODE:[MessageHandler(filters.TEXT & ~filters.COMMAND,student_edit_code)],EDIT_NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND,student_edit_name)]},fallbacks=[CommandHandler('cancel',cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(student_delete_start,pattern='^delete_student$')],states={DELETE_CODE:[MessageHandler(filters.TEXT & ~filters.COMMAND,student_delete_code)]},fallbacks=[CommandHandler('cancel',cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(reactivate_student_start,pattern='^reactivate_student$')],states={EDIT_CODE:[MessageHandler(filters.TEXT & ~filters.COMMAND,reactivate_student_code)]},fallbacks=[CommandHandler('cancel',cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(reset_attempt_start,pattern='^reset_attempt$')],states={DELETE_CODE:[MessageHandler(filters.TEXT & ~filters.COMMAND,reset_attempt_student)],EDIT_NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND,reset_attempt_exam)]},fallbacks=[CommandHandler('cancel',cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(new_exam_start,pattern='^new_exam$')],states={EXAM_TITLE:[MessageHandler(filters.TEXT & ~filters.COMMAND,new_exam_title)],EXAM_SHARED_CODE:[MessageHandler(filters.TEXT & ~filters.COMMAND,new_exam_shared_code)],EXAM_START:[MessageHandler(filters.TEXT & ~filters.COMMAND,new_exam_start_dt)],EXAM_END:[MessageHandler(filters.TEXT & ~filters.COMMAND,new_exam_end_dt)],EXAM_DURATION:[MessageHandler(filters.TEXT & ~filters.COMMAND,new_exam_duration)]},fallbacks=[CommandHandler('cancel',cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(add_question_start,pattern='^add_question$')],states={Q_EXAM:[CallbackQueryHandler(question_exam_selected,pattern='^qexam_')],Q_TEXT:[MessageHandler(filters.TEXT & ~filters.COMMAND,question_text)],Q_TYPE:[MessageHandler(filters.TEXT & ~filters.COMMAND,question_type)],Q_OPTIONS:[MessageHandler(filters.TEXT & ~filters.COMMAND,question_options)],Q_ANSWER:[MessageHandler(filters.TEXT & ~filters.COMMAND,question_answer)],Q_POINTS:[MessageHandler(filters.TEXT & ~filters.COMMAND,question_points)]},fallbacks=[CommandHandler('cancel',cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler('grade',grade_start)],states={GRADE_VALUE:[MessageHandler(filters.TEXT & ~filters.COMMAND,grade_value)]},fallbacks=[CommandHandler('cancel',cancel)]))
    app.add_handler(CallbackQueryHandler(scores,pattern='^scores$'))
    app.add_handler(CallbackQueryHandler(show_codes,pattern='^codes$'))
    app.add_handler(CallbackQueryHandler(show_results,pattern='^results$'))
    app.add_handler(CallbackQueryHandler(home,pattern='^home$'))
    # Essay answers are accepted only while an exam attempt is active.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,essay_received),group=1)
    log.info('Exam bot is running')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=='__main__': main()
