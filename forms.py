from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, SelectField, BooleanField
from wtforms.validators import DataRequired, EqualTo, Length

class LoginForm(FlaskForm):
    username = StringField('用户名', validators=[DataRequired()])
    password = PasswordField('密码', validators=[DataRequired()])
    submit = SubmitField('登录')

class RegisterForm(FlaskForm):
    username = StringField('用户名', validators=[DataRequired(), Length(min=3, max=20)])
    password = PasswordField('密码', validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField('确认密码', 
                                    validators=[DataRequired(), EqualTo('password')])
    invite_code = StringField('邀请码', validators=[DataRequired()])
    submit = SubmitField('注册')

class ChannelForm(FlaskForm):
    name = StringField('频道名称', validators=[DataRequired(), Length(min=3, max=20)])
    channel_type = SelectField('频道类型', 
                              choices=[('text', '文字频道'), ('voice', '语音频道')],
                              validators=[DataRequired()])
    is_private = BooleanField('设为私有频道')
    submit = SubmitField('创建频道')

class SettingsForm(FlaskForm):
    avatar_url = StringField('头像 URL', validators=[Length(max=255)]) # URLField 也可以考虑，但 StringField 更通用
    auto_join_voice = BooleanField('自动加入语音频道')
    submit = SubmitField('保存设置') 