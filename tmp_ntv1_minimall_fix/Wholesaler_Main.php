<?php if (!defined('BASEPATH')) exit('No direct script access allowed');

class Main extends CI_Controller {
	private $view_data = [];
    
	function __construct()
	{
		parent::__construct();
		
		$this->load->model('main_m');
		$this->load->model('common_m');
		$this->load->model('goods_m');
		$this->load->model('mypage_m');

		$this->load->library('tank_auth');
		$this->load->library('user_agent');

		if(!$this->session->userdata('user_id')) 
		{
			redirect('/auth/login/');
		}

        $this->view_data = $this->session->all_userdata();
		if(!isset($this->view_data['userid'])) {
			$info_result = $this->common_m->get_user_info($this->view_data['user_id']);
			$this->view_data['userid'] = $info_result->userid;
			$this->view_data['username'] = $info_result->username;
			$this->view_data['nickname'] = $info_result->nickname;
			$this->view_data['auth_code'] = $info_result->auth_code;
			$this->view_data['down_level'] = $info_result->down_level;
			$this->view_data['status'] = ($info_result->activated == 1)?'1': '0';
			$this->view_data['unregister_yn'] = $info_result->unregister_yn;
		}
        $this->view_data['is_mobile'] = 'N';
        if ($this->agent->is_mobile()) {
			$this->view_data['is_mobile'] = 'Y';
		}
	}

	// 도매 메인
	function index() {
		$this->load->helper('html');
		
		if((!isset($_REQUEST['mall']) || !isset($_REQUEST['sso_done'])) && $_SERVER['HTTP_HOST'] == 'pick.newtalk.kr') {
			$user_info = $this->common_m->get_user_info($this->view_data['user_id']);
			$returnUrl = '/wholesaler/main?sso_done=1';
			$userid = htmlspecialchars($user_info->userid, ENT_QUOTES, 'UTF-8');
			$username = htmlspecialchars($user_info->username, ENT_QUOTES, 'UTF-8');

			echo
				'<body onload="document.gForm.submit()">
				<form method="post" action="https://'.$userid.'.newtalk.kr/auth/pick_direct_login_main" name="gForm" style="display:none">
				<input type="hidden" name="userid" value="'.$userid.'">
				<input type="hidden" name="username" value="'.$username.'">
				<input type="hidden" name="returnUrl" value="'.$returnUrl.'">
				</form>
				</body>';
			return;
		}


		$this->view_data['user_created'] = $this->common_m->get_user_info($this->view_data['user_id'])->created;

		if (!isset($this->view_data['unregister_yn'])) {
			$this->view_data['unregister_yn'] = 'N';
		}

		


		if ($this->view_data['unregister_yn'] == 'Y') {
			$this->load->view('/auth/trans_dormancy', $this->view_data);
		} else {
			if ($this->view_data['down_level'] == '1') {
				$this->view_data['show_mall_yn'] = 'Y';
				
				$userinfo = $this->mypage_m->userinfo_get($this->view_data['user_id']);
				if ($userinfo->man_read_yn == 'N') {
					$this->mypage_m->man_read($this->view_data['user_id']);
					$this->load->view('wholesaler/main/manual', $this->view_data);
				} else {
					$pickup_popup = $this->common_m->get_pickup_popup($this->view_data['user_id']);
					$popup_list = $this->common_m->get_popup_list($this->view_data['user_id'], '1');
					if($popup_list) {
						$this->view_data['popup_list'] = $popup_list;
					}
					$this->view_data['popup_image_dir_url'] = $this->config->item('popup_image_dir_url');
					$this->view_data['pick_popup'] = $pickup_popup;
					$this->load->view('wholesaler/main/mall', $this->view_data);
				}
			} else {
				$pickup_popup = $this->common_m->get_pickup_popup($this->view_data['user_id']);
				$popup_list = $this->common_m->get_popup_list($this->view_data['user_id'], '1');
				$banner_list = $this->main_m->get_banner_list();

				$main_view_data = array();

				$main_view_data["banner_list"] = $banner_list;
				
				if($popup_list) {
					$main_view_data['popup_list'] = $popup_list;
				}
				$main_view_data['popup_image_dir_url'] = $this->config->item('popup_image_dir_url');
				$main_view_data['pick_popup'] = $pickup_popup;
				$main_view_data['show_mall_yn'] = 'N';

				// 도매 회원 로그인에 미니몰 승인 여부가 비활성화일 경우
//				if($this->view_data['activated'] == '0' && $this->view_data['auth_code'] == '4'){
					$this->load->view('wholesaler/main/main', $main_view_data);
//				}else{
//					$this->load->view('wholesaler/main/mall', $this->view_data);
//				}
				
			}
		}
	}

//	// 도매 메인
//	function send_again_free() {
//		$this->load->helper('html');
//				
//				$pickup_popup = $this->common_m->get_pickup_popup($this->view_data['user_id']);
//				$popup_list = $this->common_m->get_popup_list($this->view_data['user_id'], '1');
//				$banner_list = $this->main_m->get_banner_list();
//
//				$main_view_data = array();
//
//				$main_view_data["banner_list"] = $banner_list;
//				
//				if($popup_list) {
//					$main_view_data['popup_list'] = $popup_list;
//				}
//				$main_view_data['popup_image_dir_url'] = $this->config->item('popup_image_dir_url');
//				$main_view_data['pick_popup'] = $pickup_popup;
//				$main_view_data['show_mall_yn'] = 'N';
//
//				// 도매 회원 로그인에 미니몰 승인 여부가 비활성화일 경우
////				if($this->view_data['activated'] == '0' && $this->view_data['auth_code'] == '4'){
//					$this->load->view('wholesaler/main/main', $main_view_data);
////				}else{
////					$this->load->view('wholesaler/main/mall', $this->view_data);
////				}
//	}

	// 팝업 다시보기
	function recycle_popup() {
		$pop_id = isset($_REQUEST['pop_id']) ? $_REQUEST['pop_id']:'';

		if($pop_id == '') {
			alert("잘못된 접근 방식입니다.");
			exit;
		}

		$this->common_m->update_popup_recycle_date($this->view_data['user_id'], $pop_id);

		echo '{"info":{"success":"true","text":"3일동안 다시보지 않기가 적용되었습니다"}}';
	}

	// 팝업 다시보지 않기
	function not_again_popup() {
		$pop_id = isset($_REQUEST['pop_id']) ? $_REQUEST['pop_id']:'';

		if($pop_id == '') {
			alert("잘못된 접근 방식입니다.");
			exit;
		}

		$this->common_m->popup_not_again($this->view_data['user_id'], $pop_id);

		echo '{"info":{"success":"true","text":"3일동안 다시보지 않기가 적용되었습니다"}}';
	}

	// 상품 카카오톡 공유
	function product_new_kakao_msg_send() {
		$user_id = isset($_REQUEST['no']) ? $_REQUEST['no']:'';

		if($user_id == '' || $user_id != $this->view_data['user_id']) {
			alert("잘못된 접근 방식입니다.");
			exit;
		}

		$result = $this->goods_m->get_product_new_list('중국사입');

		$msg = "♥#{소매회원}♥고객님~^^

		여성의류 도매 ♠#{도매회원}♠에서 요청하신 샘플상품 리스트입니다.
		상품 확인하시고 문의 바랍니다.~^^
		
		★샘플상품 리스트★
		
		 #{상품1}
		
		 #{상품2}
		
		 #{상품3}
		
		▶사이트내 ◈카톡버튼 클릭시 바로 문의 가능합니다. 
		샘플요청, 주문/제작/오더의뢰 등등 문의시 ☞빠른 답변드립니다.";

		$msg = str_replace('#{소매회원}', '소매', $msg);
		$msg = str_replace('#{도매회원}', '중국사입', $msg);

		if($result) {
			foreach($result as $key => $value) {
				$msg = str_replace('#{상품'.(intval($key) + 1).'}', $value['GoodsEtc5'].'['.number_format($value['GoodsEtc9']).'] : https://test111.newtalk.kr/goods/detail/'.$value['id'], $msg);
			}
		}

		$message['msg'] = $msg;
		$json_msg = json_encode($message, JSON_UNESCAPED_UNICODE);

		echo '{"info":{"success":"true","text":"성공","data":'.$json_msg.'}}';
	}
}

/* End of file Main.php */
/* Location: ./application/controllers/wholesaler/Mian.php */
