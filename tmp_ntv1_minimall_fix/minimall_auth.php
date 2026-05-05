<?php if (!defined('BASEPATH')) exit('No direct script access allowed');

class Auth extends CI_Controller
{
	function __construct()
	{
		parent::__construct();
		//Console::log($this);

		$this->load->config('tank_auth', TRUE);
		$this->load->helper(array('form', 'url'));
		$this->load->library('form_validation');
		$this->load->library('tank_auth');
		$this->lang->load('tank_auth');
	}

	function index()
	{
		//redirect('/auth/login/');
		redirect('/main');
	}

	/**
	 * Login user on the site
	 *
	 * @return void
	 */
	function login()
	{
		//debug($this->session->all_userdata());
		//debug($this->input->post());

		if(!@$this->uri->segment(3)) {
			$returnUrl = '/main';
		} else {
			$returnUrl = @$this->uri->segment(3);
		}
		//debug($returnUrl);

		if ($this->tank_auth->is_logged_in()) {									// logged in
			redirect('/main');

		} elseif ($this->tank_auth->is_logged_in(FALSE)) {						// logged in, not activated
			//redirect('/auth/send_again/');
			alert('승인된 회원이 아닙니다!', '/auth/login');

		} else {
			//debug($returnUrl);
			$data['login_by_username'] = ($this->config->item('login_by_username', 'tank_auth') AND
					$this->config->item('use_username', 'tank_auth'));
			$data['login_by_email'] = $this->config->item('login_by_email', 'tank_auth');

			$this->form_validation->set_rules('login', '아이디', 'trim|required|xss_clean');
			$this->form_validation->set_rules('password', '비밀번호', 'trim|required|xss_clean');
			//$this->form_validation->set_rules('remember', '자동로그인', 'integer');

			// Get login for counting attempts to login
			if ($this->config->item('login_count_attempts', 'tank_auth') AND ($login = $this->input->post('login'))) {
				$login = $this->security->xss_clean($login);
			} else {
				$login = '';
			}

			$data['use_recaptcha'] = $this->config->item('use_recaptcha', 'tank_auth');
			if ($this->tank_auth->is_max_login_attempts_exceeded($login)) {
				if ($data['use_recaptcha'])
					$this->form_validation->set_rules('recaptcha_response_field', 'Confirmation Code', 'trim|xss_clean|required|callback__check_recaptcha');
				/*
				else
					$this->form_validation->set_rules('captcha', 'Confirmation Code', 'trim|xss_clean|required|callback__check_captcha');
				*/
			}
			$data['errors'] = array();
			//debug($data);

			if ($this->form_validation->run()) {								// validation ok
				//debug($returnUrl);
				if ($this->tank_auth->login(
						$this->form_validation->set_value('login'),
						$this->form_validation->set_value('password'),
						$this->form_validation->set_value('remember'),
						$data['login_by_username'],
						$data['login_by_email'])) {								// success

					//debug($this->session->all_userdata());
					//exit;
					// 로그인 후 페이지 리다이렉트 관련 수정. by 웅파. 2013.03.05
					redirect($returnUrl);
					//redirect(unserialize(gzuncompress(stripslashes(base64_decode(strtr($returnUrl, '-_.', '+/='))))));
				} else {
					$errors = $this->tank_auth->get_error_message();
					if (isset($errors['banned'])) {								// banned user
						$this->_show_message($this->lang->line('auth_message_banned').' '.$errors['banned']);
						return;

					} elseif (isset($errors['not_activated'])) {				// not activated user
						//redirect('/auth/send_again/');
						alert('승인된 회원이 아닙니다!', '/auth/login');

					} else {													// fail
						foreach ($errors as $k => $v)	$data['errors'][$k] = $this->lang->line($v);
					}
					//debug($this->form_validation);
					//Console::log($data);
				}
			}
			$data['show_captcha'] = FALSE;
			if ($this->tank_auth->is_max_login_attempts_exceeded($login)) {
				$data['show_captcha'] = TRUE;
				if ($data['use_recaptcha']) {
					$data['recaptcha_html'] = $this->_create_recaptcha();
				} else {
					$data['captcha_html'] = $this->_create_captcha();
				}
			}
			//Console::log($errors);

			$this->load->view('top');
			$this->load->view('auth/login_form', $data);
			$this->load->view('bottom');
		}
		//Console::log(_get_validation_object());

	}

	/**
	 * Login user on the site
	 *
	 * @return void
	 */
	function pick_direct_login()
	{
        // debug('pick_direct_login');
		//debug($this->session->all_userdata());exit;
		//if($this->session->userdata('auth_code') != 15) return;

		//$save_path = $this->config->item('market');
		// debug($this->input->get('user'));
		// debug($this->uri->segment(3));

        $returnUrl = $this->input->post('returnUrl');
        
        $host = (strrpos($_SERVER["HTTP_HOST"], 'ddg.kr')) ? 'https://pick.ddg.kr':'https://pick.newtalk.kr';
        
        if(!$this->input->post('userid')) redirect($host.$returnUrl);
        if(!$this->input->post('userpw')) redirect($host.$returnUrl);

        $user_id = $this->input->post('userid');
        $user_pw = $this->input->post('userpw');
		// debug($user_id);

		$this->load->model('tank_auth/users');

		// if(!$this->uri->segment(3)) return;
		if($user_id =='autoscrap' || $user_id =='danharoo') redirect($host.$returnUrl);

		//session_save_path($save_path['path_cookie_nm']);
		//ini_set('session.gc_probability', 1);
		//$this->config->item('cookie_domain', $this->uri->segment(3).'.newtalk.kr');

        if ($this->tank_auth->login(
                $user_id,
                $user_pw,
                '',
                '',
                '')) 
        {	
            $user = $this->users->get_user_by_login(urldecode($user_id));
            // debug($user);
            if(!$user->id) redirect($host.$returnUrl);

            $this->session->set_userdata(array(
                    'user_id'	=> $user->id,
                    'userid'	=> $user->userid,
                    'username'	=> $user->username,
                    'nickname'	=> $user->nickname,
                    'auth_code'	=> $user->auth_code,
                    'down_level'=> $user->down_level,
                    'status'	=> ($user->activated == 1) ? $user->activated : 0,
            ));

        }

        redirect($host.$returnUrl);
        // echo 'this';
		//$this->load->view('auth/direct_login');
	}

	// pick main 로그인, 2022.02.07 이명석
	function pick_direct_login_main() {
		$returnUrl = $this->input->post('returnUrl');
		
		$host = (strrpos($_SERVER["HTTP_HOST"], 'ddg.kr')) ? 'https://pick.ddg.kr':'https://pick.newtalk.kr';
		if(!$returnUrl) $returnUrl = '/main';
		$mallUrl = $host.$returnUrl.((strpos($returnUrl, '?') === false) ? '?' : '&').'mall=1';
		
		if(!$this->input->post('userid')) redirect($mallUrl);
		if(!$this->input->post('username')) redirect($mallUrl);

		$user_id = $this->input->post('userid');
		$user_name = $this->input->post('username');

		$this->load->model('tank_auth/users');

		if($user_id =='autoscrap' || $user_id =='danharoo') redirect($mallUrl);
		
		if ((strlen($user_id) > 0) AND (strlen($user_name) > 0)) {
			if (!is_null($user = $this->users->get_user_by_login($user_id))) {	// login ok
				if($user_name == $user->username) {	// name ok
					if ($user->banned == 1) {
						redirect($mallUrl);
						}else {
							if ($user->activated == 0) {							// fail - not activated
								redirect($mallUrl);
							}else {												// success
								$this->session->set_userdata(array(
									'user_id'	=> $user->id,
									'userid'	=> $user->userid,
									'username'	=> $user->username,
									'nickname'	=> $user->nickname,
									'auth_code'	=> $user->auth_code,
									'down_level'=> $user->down_level,
									'status'	=> ($user->activated == 1) ? $user->activated : 0,
							));

							if ($this->config->item('login_count_attempts', 'tank_auth')) {
								$this->load->model('tank_auth/login_attempts');
								$this->login_attempts->clear_attempts(
									$this->input->ip_address(),
									$user_id,
									$this->config->item('login_attempt_expire', 'tank_auth'));
							}

							$this->users->update_login_info(
									$user->id,
									$this->config->item('login_record_ip', 'tank_auth'),
									$this->config->item('login_record_time', 'tank_auth')
							);
						}
					}
				}
			}
		}

		redirect($mallUrl);
	}

	function login_gate()
	{
		if(!@$this->uri->segment(3)) {
			$returnUrl = '';
		} else {
			$returnUrl = @$this->uri->segment(3);
		}

		if ($this->tank_auth->is_logged_in()) {									// logged in
			redirect('/main');

		} elseif ($this->tank_auth->is_logged_in(FALSE)) {						// logged in, not activated
			alert('승인된 회원이 아닙니다!', '/auth/login');
			//redirect('/auth/send_again/');

		} else {
			$data['login_by_username'] = ($this->config->item('login_by_username', 'tank_auth') AND
					$this->config->item('use_username', 'tank_auth'));
			$data['login_by_email'] = $this->config->item('login_by_email', 'tank_auth');

			$this->form_validation->set_rules('login', '아이디', 'trim|required|xss_clean');
			$this->form_validation->set_rules('password', '비밀번호', 'trim|required|xss_clean');
			$this->form_validation->set_rules('remember', '자동로그인', 'integer');

			// Get login for counting attempts to login
			if ($this->config->item('login_count_attempts', 'tank_auth') AND
					($login = $this->input->post('login'))) {
				$login = $this->security->xss_clean($login);
			} else {
				$login = '';
			}
			//debug($login);

			$data['use_recaptcha'] = $this->config->item('use_recaptcha', 'tank_auth');
			if ($this->tank_auth->is_max_login_attempts_exceeded($login)) {
				if ($data['use_recaptcha'])
					$this->form_validation->set_rules('recaptcha_response_field', 'Confirmation Code', 'trim|xss_clean|required|callback__check_recaptcha');
				else
					$this->form_validation->set_rules('captcha', 'Confirmation Code', 'trim|xss_clean|required|callback__check_captcha');
			}
			$data['errors'] = array();

			if ($this->form_validation->run()) {								// validation ok
				if ($this->tank_auth->login(
						$this->form_validation->set_value('login'),
						$this->form_validation->set_value('password'),
						$this->form_validation->set_value('remember'),
						$data['login_by_username'],
						$data['login_by_email'])) {								// success

					// 로그인 후 페이지 리다이렉트 관련 수정
					// modified by kwangmyung, choi at 2009.06.29
					// redirect('');
					redirect(base64_decode($returnUrl));
				} else {
					$errors = $this->tank_auth->get_error_message();
					if (isset($errors['banned'])) {								// banned user
						$this->_show_message($this->lang->line('auth_message_banned').' '.$errors['banned']);
						return;

					} elseif (isset($errors['not_activated'])) {				// not activated user
						//redirect('/auth/send_again/');
						alert('승인된 회원이 아닙니다!', '/auth/login');

					} else {													// fail
						foreach ($errors as $k => $v)	$data['errors'][$k] = $this->lang->line($v);
					}
				}
			}
			$data['show_captcha'] = FALSE;
			if ($this->tank_auth->is_max_login_attempts_exceeded($login)) {
				$data['show_captcha'] = TRUE;
				if ($data['use_recaptcha']) {
					$data['recaptcha_html'] = $this->_create_recaptcha();
				} else {
					$data['captcha_html'] = $this->_create_captcha();
				}
			}
			$this->load->view('auth/login_form_gate', $data);
		}
	}

	/**
	 * Logout user
	 *
	 * @return void
	 */
	function logout()
	{
		$this->tank_auth->logout();
		//alert($this->lang->line('auth_message_logged_out'), '/');
		redirect('/auth', 'refresh');
	}

	/**
	 * Register user on the site
	 *
	 * @return void
	 */
	function register()
	{		
		redirect('https://pick.newtalk.kr/auth/register');exit;
		redirect('http://pigup.newtalk.kr/auth/register');exit;

		if ($this->tank_auth->is_logged_in()) {									// logged in
			redirect('/main');

		} elseif ($this->tank_auth->is_logged_in(FALSE)) {						// logged in, not activated
			//redirect('/auth/send_again/');
			alert('승인된 회원이 아닙니다!', '/auth/login');

		} elseif (!$this->config->item('allow_registration', 'tank_auth')) {	// registration is off
			$this->_show_message($this->lang->line('auth_message_registration_disabled'));
			return;

		} else {
			//debug($_POST);
			//$use_username = $this->config->item('use_username', 'tank_auth');

			$this->form_validation->set_rules('userid', '아이디', 'trim|required|xss_clean|min_length['.$this->config->item('username_min_length', 'tank_auth').']|max_length['.$this->config->item('username_max_length', 'tank_auth').']|alpha_dash');
			$this->form_validation->set_rules('username', '사용자명', 'trim|required|xss_clean');
			$this->form_validation->set_rules('password', '비밀번호', 'trim|required|xss_clean|min_length['.$this->config->item('password_min_length', 'tank_auth').']|max_length['.$this->config->item('password_max_length', 'tank_auth').']');
			$this->form_validation->set_rules('confirm_password', '비밀번호 재입력', 'trim|required|xss_clean|matches[password]');
			$this->form_validation->set_rules('hp', '휴대폰번호', 'trim|required|xss_clean');
			$this->form_validation->set_rules('email', '이메일', 'trim|required|xss_clean|valid_email');
			$this->form_validation->set_rules('shop_name', '매장및 쇼핑몰명', 'trim|required|xss_clean');
			$this->form_validation->set_rules('shop_addr', '사업장주소', 'trim|required|xss_clean');
			$this->form_validation->set_rules('shop_tel', '매장연락처', 'trim|required|xss_clean');
			$this->form_validation->set_rules('shop_taxid', '사업자번호', 'trim|required|xss_clean');

			$captcha_registration	= $this->config->item('captcha_registration', 'tank_auth');
			$use_recaptcha			= $this->config->item('use_recaptcha', 'tank_auth');
			if ($captcha_registration) {
				if ($use_recaptcha) {
					$this->form_validation->set_rules('recaptcha_response_field', '인증코드', 'trim|xss_clean|required|callback__check_recaptcha');
				} else {
					$this->form_validation->set_rules('captcha', '인증코드', 'trim|xss_clean|required|callback__check_captcha');
				}
			}
			$data['errors'] = array();

			$email_activation = $this->config->item('email_activation', 'tank_auth');

			if ($this->form_validation->run()) {								// validation ok
				if (!is_null($data = $this->tank_auth->create_user(
						$this->form_validation->set_value('userid'),
						$this->form_validation->set_value('username'),
						$this->form_validation->set_value('password'),
						$this->form_validation->set_value('hp'),
						$this->form_validation->set_value('email'),
						$this->form_validation->set_value('shop_name'),
						$this->form_validation->set_value('shop_addr'),
						$this->form_validation->set_value('shop_tel'),
						$this->form_validation->set_value('shop_taxid'),
						$this->config->item('subdomain_name'),
						$email_activation))) {									// success

					$data['site_name'] = $this->config->item('website_name', 'tank_auth');

					if ($email_activation) {									// send "activate" email
						$data['activation_period'] = $this->config->item('email_activation_expire', 'tank_auth') / 3600;

						$this->_send_email('activate', $data['email'], $data);

						unset($data['password']); // Clear password (just for any case)

						$this->_show_message($this->lang->line('auth_message_registration_completed_1'));
						return;

					} else {
						if ($this->config->item('email_account_details', 'tank_auth')) {	// send "welcome" email

							$this->_send_email('welcome', $data['email'], $data);
						}
						unset($data['password']); // Clear password (just for any case)

						//$this->load->view('top_v');
						//$this->_show_message($this->lang->line('auth_message_registration_completed_2').' '.anchor(site_url('/auth/login/'), 'Login'));
						//$this->load->view('bottom_v');
						redirect('auth');
						return;
					}
				} else {
					$errors = $this->tank_auth->get_error_message();
					foreach ($errors as $k => $v)	$data['errors'][$k] = $this->lang->line($v);
				}
			}
			if ($captcha_registration) {
				if ($use_recaptcha) {
					$data['recaptcha_html'] = $this->_create_recaptcha();
				} else {
					$data['captcha_html'] = $this->_create_captcha();
				}
			}
//			$data['use_username'] = $use_username;
			$data['captcha_registration'] = $captcha_registration;
			$data['use_recaptcha'] = $use_recaptcha;

			$this->load->view('top');
			$this->load->view('auth/register_form', $data);
			$this->load->view('bottom');
		}
	}

	/**
	 * Modify user on the site
	 *
	 * @return void
	 */
	function modify()
	{
		if (!$this->tank_auth->is_logged_in()) {									// logout
			$aa = base64_encode('auth/modify');
			redirect('/auth/login/'.$aa);

		} else {
			//$this->form_validation->set_rules('nickname', '별명', 'trim|required|xss_clean');
			$this->form_validation->set_rules('email', '이메일', 'trim|required|xss_clean|valid_email');
			$this->form_validation->set_rules('password', '비밀번호', 'trim|required|xss_clean|min_length['.$this->config->item('password_min_length', 'tank_auth').']|max_length['.$this->config->item('password_max_length', 'tank_auth').']|alpha_dash');
			$this->form_validation->set_rules('confirm_password', '비밀번호 재입력', 'trim|required|xss_clean|matches[password]');

			if ($this->form_validation->run()) {								// validation ok

				$hasher = new PasswordHash(PHPASS_HASH_STRENGTH, PHPASS_HASH_PORTABLE);
				$hashed_password = $hasher->HashPassword($this->input->post('password'));

				$data = array(
							   //'nickname' => $this->input->post('nickname'),
							   'email' => $this->input->post('email'),
							   'password' => $hashed_password
							);

				$this->db->where('id', $this->tank_auth->get_user_id());
				$this->db->update('users', $data);

				redirect('/auth/modify');

			}

			$this->db->select("*");
			$this->db->where('id', $this->tank_auth->get_user_id());
			$query = $this->db->get('users');
			$data['views'] = $views= $query->row_array();


			$this->load->view('top_v');
			$this->load->view('auth/modify_form', $data);
			$this->load->view('bottom_v');
		}
	}


	/**
	 * Send activation email again, to the same or new email address
	 *
	 * @return void
	 */
	function send_again()
	{
		if (!$this->tank_auth->is_logged_in(FALSE)) {							// not logged in or activated
			$aa = base64_encode('auth/change_password');
			redirect('/auth/login/'.$aa);

		} else {
			$this->form_validation->set_rules('email', 'Email', 'trim|required|xss_clean|valid_email');

			$data['errors'] = array();

			if ($this->form_validation->run()) {								// validation ok
				if (!is_null($data = $this->tank_auth->change_email(
						$this->form_validation->set_value('email')))) {			// success

					$data['site_name']	= $this->config->item('website_name', 'tank_auth');
					$data['activation_period'] = $this->config->item('email_activation_expire', 'tank_auth') / 3600;

					$this->_send_email('activate', $data['email'], $data);

					$this->_show_message(sprintf($this->lang->line('auth_message_activation_email_sent'), $data['email']));
					return;
				} else {
					$errors = $this->tank_auth->get_error_message();
					foreach ($errors as $k => $v)	$data['errors'][$k] = $this->lang->line($v);
				}
			}

			//$this->load->view('top');
			$this->load->view('auth/send_again_form', $data);
			//$this->load->view('bottom');
		}
	}

	/**
	 * Activate user account.
	 * User is verified by user_id and authentication code in the URL.
	 * Can be called by clicking on link in mail.
	 *
	 * @return void
	 */
	function activate()
	{
		$user_id		= $this->uri->segment(3);
		$new_email_key	= $this->uri->segment(4);

		// Activate user

		$this->load->view('top_v');
		if ($this->tank_auth->activate_user($user_id, $new_email_key)) {		// success
			$this->tank_auth->logout();
			$this->_show_message($this->lang->line('auth_message_activation_completed').' '.anchor(site_url('/auth/login/'), 'Login'));

		} else {																// fail
			$this->_show_message($this->lang->line('auth_message_activation_failed'));
		}
		$this->load->view('bottom_v');
	}

	/**
	 * Generate reset code (to change password) and send it to user
	 *
	 * @return void
	 */
	function forgot_password()
	{
		if ($this->tank_auth->is_logged_in()) {									// logged in
			redirect('/');

		} elseif ($this->tank_auth->is_logged_in(FALSE)) {						// logged in, not activated
			redirect('/auth/send_again/');

		} else {
			$this->form_validation->set_rules('login', 'Email or login', 'trim|required|xss_clean');

			$data['errors'] = array();

			if ($this->form_validation->run()) {								// validation ok
				if (!is_null($data = $this->tank_auth->forgot_password(
						$this->form_validation->set_value('login')))) {

					$data['site_name'] = $this->config->item('website_name', 'tank_auth');

					// Send email with password activation link
					$this->_send_email('forgot_password', $data['email'], $data);

					$this->_show_message($this->lang->line('auth_message_new_password_sent'));
					return;

				} else {
					$errors = $this->tank_auth->get_error_message();
					foreach ($errors as $k => $v)	$data['errors'][$k] = $this->lang->line($v);
				}
			}
			$this->load->view('top_v');
			$this->load->view('auth/forgot_password_form', $data);
			$this->load->view('bottom_v');
		}
	}

	/**
	 * Replace user password (forgotten) with a new one (set by user).
	 * User is verified by user_id and authentication code in the URL.
	 * Can be called by clicking on link in mail.
	 *
	 * @return void
	 */
	function reset_password()
	{
		$user_id		= $this->uri->segment(3);
		$new_pass_key	= $this->uri->segment(4);

		$this->form_validation->set_rules('new_password', 'New Password', 'trim|required|xss_clean|min_length['.$this->config->item('password_min_length', 'tank_auth').']|max_length['.$this->config->item('password_max_length', 'tank_auth').']|alpha_dash');
		$this->form_validation->set_rules('confirm_new_password', 'Confirm new Password', 'trim|required|xss_clean|matches[new_password]');

		$data['errors'] = array();

		if ($this->form_validation->run()) {								// validation ok
			if (!is_null($data = $this->tank_auth->reset_password(
					$user_id, $new_pass_key,
					$this->form_validation->set_value('new_password')))) {	// success

				$data['site_name'] = $this->config->item('website_name', 'tank_auth');

				// Send email with new password
				$this->_send_email('reset_password', $data['email'], $data);

				$this->_show_message($this->lang->line('auth_message_new_password_activated').' '.anchor(site_url('/auth/login/'), 'Login'));
				return;

			} else {														// fail
				$this->_show_message($this->lang->line('auth_message_new_password_failed'));
				return;
			}
		} else {
			if (!$this->tank_auth->can_reset_password($user_id, $new_pass_key)) {
				$this->_show_message($this->lang->line('auth_message_new_password_failed'));
				return;
			}
		}
		$this->load->view('top_v');
		$this->load->view('auth/reset_password_form', $data);
		$this->load->view('bottom_v');
	}

	/**
	 * Change user password
	 *
	 * @return void
	 */
	function change_password()
	{
		if (!$this->tank_auth->is_logged_in()) {								// not logged in or not activated
			$aa = base64_encode('auth/change_password');
			redirect('/auth/login/'.$aa);

		} else {
			$this->form_validation->set_rules('old_password', 'Old Password', 'trim|required|xss_clean');
			$this->form_validation->set_rules('new_password', 'New Password', 'trim|required|xss_clean|min_length['.$this->config->item('password_min_length', 'tank_auth').']|max_length['.$this->config->item('password_max_length', 'tank_auth').']|alpha_dash');
			$this->form_validation->set_rules('confirm_new_password', 'Confirm new Password', 'trim|required|xss_clean|matches[new_password]');

			$data['errors'] = array();

			if ($this->form_validation->run()) {								// validation ok
				if ($this->tank_auth->change_password(
						$this->form_validation->set_value('old_password'),
						$this->form_validation->set_value('new_password'))) {	// success
					$this->_show_message($this->lang->line('auth_message_password_changed'));
					return;

				} else {														// fail
					$errors = $this->tank_auth->get_error_message();
					foreach ($errors as $k => $v)	$data['errors'][$k] = $this->lang->line($v);
				}
			}
			$this->load->view('top_v');
			$this->load->view('auth/change_password_form', $data);
			$this->load->view('bottom_v');
		}
	}

	/**
	 * Change user email
	 *
	 * @return void
	 */
	function change_email()
	{
		if (!$this->tank_auth->is_logged_in()) {								// not logged in or not activated
			$aa = base64_encode('/auth/change_email');
			redirect('/auth/login/'.$aa);
		} else {
			$this->form_validation->set_rules('password', 'Password', 'trim|required|xss_clean');
			$this->form_validation->set_rules('email', 'Email', 'trim|required|xss_clean|valid_email');

			$data['errors'] = array();

			if ($this->form_validation->run()) {								// validation ok
				if (!is_null($data = $this->tank_auth->set_new_email(
						$this->form_validation->set_value('email'),
						$this->form_validation->set_value('password')))) {			// success

					$data['site_name'] = $this->config->item('website_name', 'tank_auth');

					// Send email with new email address and its activation link
					$this->_send_email('change_email', $data['new_email'], $data);

					$this->_show_message(sprintf($this->lang->line('auth_message_new_email_sent'), $data['new_email']));
					return;

				} else {
					$errors = $this->tank_auth->get_error_message();
					foreach ($errors as $k => $v)	$data['errors'][$k] = $this->lang->line($v);
				}
			}
			$this->load->view('top_v');
			$this->load->view('auth/change_email_form', $data);
			$this->load->view('bottom_v');
		}
	}

	/**
	 * Replace user email with a new one.
	 * User is verified by user_id and authentication code in the URL.
	 * Can be called by clicking on link in mail.
	 *
	 * @return void
	 */
	function reset_email()
	{
		$user_id		= $this->uri->segment(3);
		$new_email_key	= $this->uri->segment(4);

		// Reset email
		$this->load->view('top_v');
		if ($this->tank_auth->activate_new_email($user_id, $new_email_key)) {	// success
			$this->tank_auth->logout();
			$this->_show_message($this->lang->line('auth_message_new_email_activated').' '.anchor(site_url('/auth/login/'), 'Login'));

		} else {																// fail
			$this->_show_message($this->lang->line('auth_message_new_email_failed'));
		}
		$this->load->view('bottom_v');
	}

	/**
	 * Delete user from the site (only when user is logged in)
	 *
	 * @return void
	 */
	function unregister()
	{
		if (!$this->tank_auth->is_logged_in()) {								// not logged in or not activated
			$aa = base64_encode('auth/unregister');
			redirect('/auth/login/'.$aa);

		} else {
			$this->form_validation->set_rules('password', 'Password', 'trim|required|xss_clean');

			$data['errors'] = array();

			if ($this->form_validation->run()) {								// validation ok
				if ($this->tank_auth->delete_user(
						$this->form_validation->set_value('password'))) {		// success
					$this->_show_message($this->lang->line('auth_message_unregistered'));
					return;

				} else {														// fail
					$errors = $this->tank_auth->get_error_message();
					foreach ($errors as $k => $v)	$data['errors'][$k] = $this->lang->line($v);
				}
			}
			$this->load->view('top_v');
			$this->load->view('auth/unregister_form', $data);
			$this->load->view('bottom_v');
		}
	}

	/**
	 * Show info message
	 *
	 * @param	string
	 * @return	void
	 */
	function _show_message($message)
	{
		$this->load->view('auth/general_message', array('message' => $message));
	}

	/**
	 * Send email message of given type (activate, forgot_password, etc.)
	 *
	 * @param	string
	 * @param	string
	 * @param	array
	 * @return	void
	 */
	function _send_email($type, $email, &$data)
	{
		$this->load->library('email');
		$this->email->from($this->config->item('webmaster_email', 'tank_auth'), $this->config->item('website_name', 'tank_auth'));
		$this->email->reply_to($this->config->item('webmaster_email', 'tank_auth'), $this->config->item('website_name', 'tank_auth'));
		$this->email->to($email);
		$this->email->subject(sprintf($this->lang->line('auth_subject_'.$type), $this->config->item('website_name', 'tank_auth')));
		$this->email->message($this->load->view('email/'.$type.'-html', $data, TRUE));
		$this->email->set_alt_message($this->load->view('email/'.$type.'-txt', $data, TRUE));
		$this->email->send();
	}

	/**
	 * Create CAPTCHA image to verify user as a human
	 *
	 * @return	string
	 */
	function _create_captcha()
	{
		$this->load->helper('captcha');

		$cap = create_captcha(array(
			'word'			=> mt_rand(10274536, 90853127),
			'img_path'		=> './'.$this->config->item('captcha_path', 'tank_auth'),
			'img_url'		=> base_url().$this->config->item('captcha_path', 'tank_auth'),
			'font_path'		=> './'.$this->config->item('captcha_fonts_path', 'tank_auth'),
			'font_size'		=> $this->config->item('captcha_font_size', 'tank_auth'),
			'img_width'		=> $this->config->item('captcha_width', 'tank_auth'),
			'img_height'	=> $this->config->item('captcha_height', 'tank_auth'),
			'show_grid'		=> $this->config->item('captcha_grid', 'tank_auth'),
			'expiration'	=> $this->config->item('captcha_expire', 'tank_auth'),
		));

		// Save captcha params in session
		$this->session->set_flashdata(array(
				'captcha_word' => $cap['word'],
				'captcha_time' => $cap['time'],
		));

		return $cap['image'];
	}

	/**
	 * Callback function. Check if CAPTCHA test is passed.
	 *
	 * @param	string
	 * @return	bool
	 */
	function _check_captcha($code)
	{
		$time = $this->session->flashdata('captcha_time');
		$word = $this->session->flashdata('captcha_word');

		list($usec, $sec) = explode(" ", microtime());
		$now = ((float)$usec + (float)$sec);

		if ($now - $time > $this->config->item('captcha_expire', 'tank_auth')) {
			$this->form_validation->set_message('_check_captcha', $this->lang->line('auth_captcha_expired'));
			return FALSE;

		} elseif (($this->config->item('captcha_case_sensitive', 'tank_auth') AND
				$code != $word) OR
				strtolower($code) != strtolower($word)) {
			$this->form_validation->set_message('_check_captcha', $this->lang->line('auth_incorrect_captcha'));
			return FALSE;
		}
		return TRUE;
	}

	/**
	 * Create reCAPTCHA JS and non-JS HTML to verify user as a human
	 *
	 * @return	string
	 */
	function _create_recaptcha()
	{
		$this->load->helper('recaptcha');

		// Add custom theme so we can get only image
		$options = "<script>var RecaptchaOptions = {theme: 'custom', custom_theme_widget: 'recaptcha_widget'};</script>\n";

		// Get reCAPTCHA JS and non-JS HTML
		$html = recaptcha_get_html($this->config->item('recaptcha_public_key', 'tank_auth'));

		return $options.$html;
	}

	/**
	 * Callback function. Check if reCAPTCHA test is passed.
	 *
	 * @return	bool
	 */
	function _check_recaptcha()
	{
		$this->load->helper('recaptcha');

		$resp = recaptcha_check_answer($this->config->item('recaptcha_private_key', 'tank_auth'),
				$_SERVER['REMOTE_ADDR'],
				$_POST['recaptcha_challenge_field'],
				$_POST['recaptcha_response_field']);

		if (!$resp->is_valid) {
			$this->form_validation->set_message('_check_recaptcha', $this->lang->line('auth_incorrect_captcha'));
			return FALSE;
		}
		return TRUE;
	}

}

/* End of file auth.php */
/* Location: ./application/controllers/auth.php */
